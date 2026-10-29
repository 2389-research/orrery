# ABOUTME: The Relay class — main interface for orrery-relay.
# ABOUTME: Wraps client creation, model mapping, retry, logging, and usage tracking.

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .backends import create_async_client, create_sync_client, map_model_id
from .retry import with_retry, with_retry_sync
from .types import RelayResponse, UsageEvent

logger = logging.getLogger("orrery_relay")


def _anthropic_to_ollama_messages(messages: list[dict], system: str | None = None) -> tuple[list[dict], str | None]:
    """Translate Anthropic message format to native Ollama /api/chat format.

    Extracts images from content blocks into the 'images' array.
    Returns (ollama_messages, system_prompt).
    """
    result = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            result.append({"role": msg["role"], "content": content})
        elif isinstance(content, list):
            text_parts = []
            images = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "image":
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            images.append(source.get("data", ""))
                    elif block.get("type") == "text":
                        text_parts.append(block["text"])
            entry = {"role": msg["role"], "content": "\n".join(text_parts)}
            if images:
                entry["images"] = images
            result.append(entry)
        else:
            result.append({"role": msg["role"], "content": str(content) if content else ""})
    return result, system


# Kwargs that only mean something to the native Ollama endpoint. They must be
# stripped before the Anthropic SDK call: `messages.create()` has a closed
# signature and raises TypeError on an unknown keyword, so a caller that passes
# `ollama_options` unconditionally (the right thing — it cannot know the backend)
# would otherwise break the moment ANTHROPIC_BACKEND is bedrock or gateway.
_OLLAMA_ONLY_KWARGS = ("ollama_options", "format")


def _strip_ollama_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _OLLAMA_ONLY_KWARGS}

def _anthropic_to_openai_messages(messages: list[dict], system: str | None = None) -> list[dict]:
    """Translate Anthropic-format messages to OpenAI Chat Completions format.

    `system` becomes a leading system message. Text blocks and base64 image blocks
    are kept (images as data-URI `image_url` parts); `cache_control` and any other
    Anthropic-only block fields are dropped, since strict OpenAI-compatible servers
    reject unknown keys. Pure: does not mutate its input.
    """
    result: list[dict] = []
    if system:
        result.append({"role": "system", "content": system})
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                        parts.append({"type": "image_url", "image_url": {"url": url}})
            result.append({"role": msg["role"], "content": parts})
        else:
            result.append({"role": msg["role"], "content": content if isinstance(content, str) else (str(content) if content else "")})
    return result


def _openai_endpoint(base_url: str) -> str:
    """Join the base URL (which already carries /v1) with /chat/completions."""
    return (base_url[:-1] if base_url.endswith("/") else base_url) + "/chat/completions"


def _openai_request_body(
    model: str, messages: list[dict], max_tokens: int, system: str | None = None,
    temperature: float | None = None, reasoning_effort: str | None = None,
    response_format: dict | None = None,
) -> dict[str, Any]:
    """Build a Chat Completions request body. Optional fields are omitted when unset."""
    body: dict[str, Any] = {
        "model": model,
        "messages": _anthropic_to_openai_messages(messages, system),
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    if response_format is not None:
        body["response_format"] = response_format
    return body


def _openai_relay_response(data: dict, model: str, latency_ms: float, max_tokens: int | None = None) -> RelayResponse:
    """Map a Chat Completions JSON response to a RelayResponse; warns on truncation."""
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    if choice.get("finish_reason") == "length":
        logger.warning("truncated model=%s finish_reason=length max_tokens=%s", model, max_tokens)
    return RelayResponse(
        raw=data, text=text,
        input_tokens=usage.get("prompt_tokens") or 0, output_tokens=usage.get("completion_tokens") or 0,
        model=model, latency_ms=latency_ms, backend="openai",
    )


def _parse_json_from_text(text: str) -> dict | None:
    """Try to extract a JSON object from text that may have markdown fences."""
    text = text.strip()
    if "```" in text:
        match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


class Relay:
    """Shared LLM client that supports Bedrock, Gateway, and Ollama backends."""

    def __init__(
        self,
        backend: str = "gateway",
        gateway_url: str = "",
        gateway_api_key: str = "",
        aws_access_key: str = "",
        aws_secret_key: str = "",
        aws_region: str = "us-east-1",
        ollama_url: str = "http://localhost:11434",
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        on_usage: Callable[[UsageEvent], Awaitable[None]] | None = None,
        reasoning_effort: str | None = None,
        timeout: float | None = None,
        http_transport: Any = None,
    ) -> None:
        self._backend = backend
        self._ollama_url = ollama_url
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._on_usage = on_usage
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        self._http_transport = http_transport

        if backend not in ("ollama", "openai"):
            self._async_client = create_async_client(
                backend=backend, gateway_url=gateway_url, gateway_api_key=gateway_api_key,
                aws_access_key=aws_access_key, aws_secret_key=aws_secret_key, aws_region=aws_region,
                ollama_url=ollama_url,
            )

        # Store for sync client creation
        self._gateway_url = gateway_url
        self._gateway_api_key = gateway_api_key
        self._aws_access_key = aws_access_key
        self._aws_secret_key = aws_secret_key
        self._aws_region = aws_region
        self._sync_client: Any = None

    @classmethod
    def from_env(cls, **overrides: Any) -> Relay:
        log_level = os.environ.get("RELAY_LOG_LEVEL", "INFO")
        logging.getLogger("orrery_relay").setLevel(getattr(logging, log_level.upper(), logging.INFO))
        explicit_backend = os.environ.get("ANTHROPIC_BACKEND", "")
        aws_key = os.environ.get("AWS_ACCESS_KEY", "")
        gateway_url = os.environ.get("GATEWAY_URL", "")
        ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        has_explicit_ollama_url = "OLLAMA_URL" in os.environ
        if explicit_backend:
            backend = explicit_backend
        elif aws_key:
            backend = "bedrock"
        elif gateway_url:
            backend = "gateway"
        elif has_explicit_ollama_url:
            backend = "ollama"
        else:
            backend = "gateway"
        kwargs: dict[str, Any] = {
            "backend": backend,
            "gateway_url": gateway_url,
            "gateway_api_key": os.environ.get("GATEWAY_API_KEY", ""),
            "aws_access_key": aws_key,
            "aws_secret_key": os.environ.get("AWS_SECRET_KEY", ""),
            "aws_region": os.environ.get("AWS_REGION", "us-east-1"),
            "ollama_url": ollama_url,
            "max_retries": int(os.environ.get("RELAY_MAX_RETRIES", "3")),
            "base_delay": float(os.environ.get("RELAY_BASE_DELAY", "1.0")),
            "max_delay": float(os.environ.get("RELAY_MAX_DELAY", "30.0")),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> Relay:
        kwargs: dict[str, Any] = {
            "backend": getattr(settings, "anthropic_backend", "gateway"),
            "gateway_url": getattr(settings, "gateway_url", ""),
            "gateway_api_key": getattr(settings, "gateway_api_key", ""),
            "aws_access_key": getattr(settings, "aws_access_key", ""),
            "aws_secret_key": getattr(settings, "aws_secret_key", ""),
            "aws_region": getattr(settings, "aws_region", "us-east-1"),
            "ollama_url": getattr(settings, "ollama_url", "http://localhost:11434"),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    async def _complete_ollama(
        self, model: str, messages: list[dict], max_tokens: int,
        system: str | None = None, temperature: float | None = None,
        **kwargs: Any,
    ) -> RelayResponse:
        """Ollama completion via native /api/chat endpoint.

        Uses the native endpoint instead of the OpenAI-compatible one because:
        - Vision (image content blocks) works correctly
        - No thinking mode issues (gemma4 ThinkingBlock bug)
        - Consistent behavior across all Ollama model families
        """
        import httpx

        ollama_messages, _ = _anthropic_to_ollama_messages(messages, system)
        if system:
            ollama_messages.insert(0, {"role": "system", "content": system})

        # Check if any message has images — num_predict option breaks vision on gemma4
        has_images = any("images" in m for m in ollama_messages)

        body: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            # Disable reasoning: thinking-capable models (e.g. gemma4:26b) otherwise
            # spend the entire num_predict budget on hidden "thinking" tokens, which
            # Ollama returns in a separate `thinking` field — leaving message.content
            # empty. Since we only read message.content, that yields blank responses
            # (classification returned {} → no domains). Ignored by non-thinking models.
            "think": False,
        }
        if not has_images:
            body["options"] = {"num_predict": max_tokens}
        if temperature is not None:
            body.setdefault("options", {})["temperature"] = temperature
        # Extra native Ollama options (e.g. num_ctx). Ollama's default context is
        # small (4096) and it truncates the prompt SILENTLY from the LEFT, so a
        # caller feeding long input (a rendered reference vocabulary, a whole file,
        # a node trace) must raise num_ctx or the model answers from a prompt whose
        # head — the instructions — has been cut off. There is no error and the
        # output still looks well-formed, which is what makes it dangerous.
        # Merged last so an explicit caller value wins.
        ollama_options = kwargs.get("ollama_options")
        if ollama_options:
            body.setdefault("options", {}).update(ollama_options)
        # Native structured output: a JSON schema in `format` makes Ollama do
        # grammar-constrained decoding, so the model CANNOT emit tokens that break
        # the schema. Passed through from complete_structured; ignored otherwise.
        response_format = kwargs.get("format")
        if response_format is not None:
            body["format"] = response_format

        start = time.monotonic()

        async def _call() -> Any:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(f"{self._ollama_url}/api/chat", json=body)
                resp.raise_for_status()
                return resp.json()

        data = await with_retry(_call, max_retries=self._max_retries, base_delay=self._base_delay, max_delay=self._max_delay)
        elapsed = (time.monotonic() - start) * 1000

        text = data.get("message", {}).get("content", "")
        input_tokens = data.get("prompt_eval_count", 0)
        output_tokens = data.get("eval_count", 0)

        logger.info("complete model=%s backend=ollama tokens=%d/%d latency=%.0fms", model, input_tokens, output_tokens, elapsed)

        response = RelayResponse(raw=data, text=text, input_tokens=input_tokens, output_tokens=output_tokens, model=model, latency_ms=elapsed, backend="ollama")

        if self._on_usage:
            from datetime import datetime, timezone
            event = UsageEvent(model=model, backend="ollama", input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=elapsed, timestamp=datetime.now(timezone.utc).isoformat(), retries=0)
            await self._on_usage(event)

        return response

    async def _complete_openai(
        self, model: str, messages: list[dict], max_tokens: int,
        system: str | None = None, temperature: float | None = None,
        **kwargs: Any,
    ) -> RelayResponse:
        """OpenAI-compatible Chat Completions (LunaRoute, OpenRouter, vLLM, OpenAI).

        Thin transport shell: the pure helpers build the body and map the response so
        the sync path can share them. Ollama-only kwargs are ignored, never sent.
        """
        import httpx

        body = _openai_request_body(model, messages, max_tokens, system, temperature, self._reasoning_effort)
        url = _openai_endpoint(self._gateway_url)
        headers = {"Authorization": f"Bearer {self._gateway_api_key}"}

        start = time.monotonic()

        async def _call() -> Any:
            async with httpx.AsyncClient(timeout=self._timeout or 120.0, transport=self._http_transport) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                return resp.json()

        try:
            data = await with_retry(_call, max_retries=self._max_retries, base_delay=self._base_delay, max_delay=self._max_delay)
        except Exception:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("failed model=%s backend=openai status=error latency=%.0fms", model, elapsed)
            raise
        elapsed = (time.monotonic() - start) * 1000

        response = _openai_relay_response(data, model, elapsed, max_tokens)
        logger.info("complete model=%s backend=openai tokens=%d/%d latency=%.0fms", model, response.input_tokens, response.output_tokens, elapsed)

        if self._on_usage:
            from datetime import datetime, timezone
            event = UsageEvent(model=model, backend="openai", input_tokens=response.input_tokens, output_tokens=response.output_tokens, latency_ms=elapsed, timestamp=datetime.now(timezone.utc).isoformat(), retries=0)
            await self._on_usage(event)

        return response

    async def complete(
        self, model: str, messages: list[dict], max_tokens: int,
        system: str | None = None, temperature: float | None = None,
        tools: list[dict] | None = None, tool_choice: dict | None = None,
        **kwargs: Any,
    ) -> RelayResponse:
        if self._backend == "ollama":
            return await self._complete_ollama(model, messages, max_tokens, system, temperature, **kwargs)
        if self._backend == "openai":
            return await self._complete_openai(model, messages, max_tokens, system, temperature, **kwargs)

        mapped_model = map_model_id(model, self._backend)
        call_kwargs: dict[str, Any] = {"model": mapped_model, "messages": messages, "max_tokens": max_tokens}
        if system is not None: call_kwargs["system"] = system
        if temperature is not None: call_kwargs["temperature"] = temperature
        if tools is not None: call_kwargs["tools"] = tools
        if tool_choice is not None: call_kwargs["tool_choice"] = tool_choice
        call_kwargs.update(_strip_ollama_kwargs(kwargs))

        start = time.monotonic()
        async def _call() -> Any:
            return await self._async_client.messages.create(**call_kwargs)

        try:
            raw = await with_retry(_call, max_retries=self._max_retries, base_delay=self._base_delay, max_delay=self._max_delay)
        except Exception:
            elapsed = (time.monotonic() - start) * 1000
            logger.error("failed model=%s status=error latency=%.0fms", model, elapsed)
            raise

        elapsed = (time.monotonic() - start) * 1000
        text = ""
        if raw.content:
            text_parts = [block.text for block in raw.content if hasattr(block, "text")]
            text = "\n".join(text_parts)
        input_tokens = raw.usage.input_tokens
        output_tokens = raw.usage.output_tokens
        # getattr: these are absent on backends/SDK versions without prompt caching.
        cache_creation = getattr(raw.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(raw.usage, "cache_read_input_tokens", 0) or 0

        logger.info("complete model=%s backend=%s tokens=%d/%d cache=%d/%d latency=%.0fms", model, self._backend, input_tokens, output_tokens, cache_creation, cache_read, elapsed)

        response = RelayResponse(raw=raw, text=text, input_tokens=input_tokens, output_tokens=output_tokens, model=model, latency_ms=elapsed, backend=self._backend, cache_creation_input_tokens=cache_creation, cache_read_input_tokens=cache_read)

        if self._on_usage:
            from datetime import datetime, timezone
            event = UsageEvent(model=model, backend=self._backend, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=elapsed, timestamp=datetime.now(timezone.utc).isoformat(), retries=0)
            await self._on_usage(event)

        return response

    async def complete_structured(
        self, model: str, messages: list[dict], max_tokens: int,
        schema: dict, tool_name: str = "structured_output",
        tool_description: str = "Return structured data",
        system: str | None = None, temperature: float | None = None,
        **kwargs: Any,
    ) -> dict:
        """LLM call that returns JSON matching a schema.

        For Anthropic/Bedrock: uses tool_use to force structured output.
        For Ollama: prompts for JSON and parses from text response.
        """
        if self._backend == "ollama":
            # Enforce the schema via Ollama's native structured output (grammar-
            # constrained decoding) — the model cannot produce invalid JSON. This
            # replaces spelling the whole schema out in the prompt, which both cost
            # tokens on a context Ollama silently truncates and only *asked* for
            # valid JSON. A short nudge still helps quality (Ollama's own guidance),
            # but `format` is what guarantees parseability.
            json_instruction = "\n\nReturn ONLY a JSON value matching the required schema — no prose, no markdown fences."
            augmented = list(messages)
            if augmented and augmented[-1]["role"] == "user":
                content = augmented[-1]["content"]
                if isinstance(content, str):
                    augmented[-1] = {**augmented[-1], "content": content + json_instruction}
                elif isinstance(content, list):
                    augmented[-1] = {**augmented[-1], "content": content + [{"type": "text", "text": json_instruction}]}

            # Drop any caller-supplied `format` so the schema is the only one in play.
            call_kwargs = {k: v for k, v in kwargs.items() if k != "format"}
            response = await self._complete_ollama(
                model, augmented, max_tokens, system, temperature, format=schema, **call_kwargs)
            parsed = _parse_json_from_text(response.text)
            return parsed if parsed is not None else {}

        # Anthropic/Bedrock: use tool_use
        tools = [{
            "name": tool_name,
            "description": tool_description,
            "input_schema": schema,
        }]
        tool_choice = {"type": "tool", "name": tool_name}

        response = await self.complete(
            model=model, messages=messages, max_tokens=max_tokens,
            system=system, temperature=temperature,
            tools=tools, tool_choice=tool_choice,
            **kwargs,
        )

        for block in response.raw.content:
            if block.type == "tool_use" and block.name == tool_name:
                return block.input

        return {}

    def _complete_ollama_sync(
        self, model: str, messages: list[dict], max_tokens: int,
        system: str | None = None, temperature: float | None = None,
        **kwargs: Any,
    ) -> RelayResponse:
        """Synchronous Ollama completion via the native /api/chat endpoint.

        Sync twin of `_complete_ollama`, and required rather than cosmetic. Ollama does
        serve an Anthropic-compatible /v1/messages endpoint, so the SDK sync client can
        reach it — but that shim leaves THINKING ENABLED. Measured on gemma4:e4b: a
        one-word request finishes reasoning and emits a text block, while any
        substantive prompt spends the entire max_tokens budget inside a `thinking`
        block (stop_reason=max_tokens) and returns NO text block at all, so `.text` is
        empty. That is the same failure `_complete_ollama` avoids with `think: False`,
        and it silently produced blank output rather than an error.

        This matters because orrery-codesum summarizes a repository through
        `complete_sync` exclusively (its traversal is synchronous), so on the shim every
        file summary would come back empty.
        """
        import httpx

        ollama_messages, _ = _anthropic_to_ollama_messages(messages, system)
        if system:
            ollama_messages.insert(0, {"role": "system", "content": system})
        has_images = any("images" in m for m in ollama_messages)

        body: dict[str, Any] = {
            "model": model, "messages": ollama_messages, "stream": False,
            "think": False,   # the whole reason this path exists — see the docstring
        }
        if not has_images:
            body["options"] = {"num_predict": max_tokens}
        if temperature is not None:
            body.setdefault("options", {})["temperature"] = temperature
        # Same passthroughs as the async path. NOTE num_ctx is a CAP, not a floor:
        # left unset, current Ollama sizes the context to the prompt (measured: a
        # 15k-token prompt counted in full), so passing a value BELOW the prompt is
        # what causes silent left-truncation. Only set it when you know the ceiling.
        ollama_options = kwargs.get("ollama_options")
        if ollama_options:
            body.setdefault("options", {}).update(ollama_options)
        response_format = kwargs.get("format")
        if response_format is not None:
            body["format"] = response_format

        start = time.monotonic()

        def _call() -> Any:
            with httpx.Client(timeout=300) as client:
                resp = client.post(f"{self._ollama_url}/api/chat", json=body)
                resp.raise_for_status()
                return resp.json()

        data = with_retry_sync(_call, max_retries=self._max_retries, base_delay=self._base_delay, max_delay=self._max_delay)
        elapsed = (time.monotonic() - start) * 1000
        text = data.get("message", {}).get("content", "")
        input_tokens = data.get("prompt_eval_count", 0)
        output_tokens = data.get("eval_count", 0)
        logger.info("complete_sync model=%s backend=ollama tokens=%d/%d latency=%.0fms",
                    model, input_tokens, output_tokens, elapsed)
        return RelayResponse(raw=data, text=text, input_tokens=input_tokens,
                             output_tokens=output_tokens, model=model,
                             latency_ms=elapsed, backend="ollama")

    def complete_sync(
        self, model: str, messages: list[dict], max_tokens: int,
        system: str | None = None, temperature: float | None = None,
        **kwargs: Any,
    ) -> RelayResponse:
        if self._backend == "ollama":
            return self._complete_ollama_sync(model, messages, max_tokens, system, temperature, **kwargs)
        if self._sync_client is None:
            self._sync_client = create_sync_client(
                backend=self._backend, gateway_url=self._gateway_url, gateway_api_key=self._gateway_api_key,
                aws_access_key=self._aws_access_key, aws_secret_key=self._aws_secret_key, aws_region=self._aws_region,
                ollama_url=self._ollama_url,
            )
        mapped_model = map_model_id(model, self._backend)
        call_kwargs: dict[str, Any] = {"model": mapped_model, "messages": messages, "max_tokens": max_tokens}
        if system is not None: call_kwargs["system"] = system
        if temperature is not None: call_kwargs["temperature"] = temperature
        call_kwargs.update(_strip_ollama_kwargs(kwargs))

        start = time.monotonic()
        def _call() -> Any:
            return self._sync_client.messages.create(**call_kwargs)
        raw = with_retry_sync(_call, max_retries=self._max_retries, base_delay=self._base_delay, max_delay=self._max_delay)

        elapsed = (time.monotonic() - start) * 1000
        text = "\n".join(b.text for b in raw.content if hasattr(b, "text")) if raw.content else ""
        cache_creation = getattr(raw.usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(raw.usage, "cache_read_input_tokens", 0) or 0
        logger.info("complete_sync model=%s backend=%s tokens=%d/%d cache=%d/%d latency=%.0fms", model, self._backend, raw.usage.input_tokens, raw.usage.output_tokens, cache_creation, cache_read, elapsed)
        return RelayResponse(raw=raw, text=text, input_tokens=raw.usage.input_tokens, output_tokens=raw.usage.output_tokens, model=model, latency_ms=elapsed, backend=self._backend, cache_creation_input_tokens=cache_creation, cache_read_input_tokens=cache_read)
