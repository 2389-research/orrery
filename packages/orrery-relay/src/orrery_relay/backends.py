# ABOUTME: Client factory and model ID mapping for the gateway, bedrock, ollama and openai backends.
# ABOUTME: Creates the right Anthropic client type and translates model names.

from __future__ import annotations

from anthropic import (
    Anthropic,
    AnthropicBedrock,
    AsyncAnthropic,
    AsyncAnthropicBedrock,
)

BEDROCK_MODEL_MAP = {
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-sonnet-4-5": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-5": "us.anthropic.claude-opus-4-5-20251101-v1:0",
}


def map_model_id(model: str, backend: str) -> str:
    """Translate a friendly model name to the backend-specific ID."""
    if backend == "bedrock":
        if "us.anthropic." in model:
            return model
        return BEDROCK_MODEL_MAP.get(model, model)
    # gateway and ollama use the model name as-is
    return model


def create_async_client(
    backend: str = "gateway",
    gateway_url: str = "",
    gateway_api_key: str = "",
    aws_access_key: str = "",
    aws_secret_key: str = "",
    aws_region: str = "us-east-1",
    ollama_url: str = "http://localhost:11434",
) -> AsyncAnthropic | AsyncAnthropicBedrock:
    """Create the appropriate async Anthropic client for the chosen backend."""
    if backend == "gateway":
        return AsyncAnthropic(api_key=gateway_api_key, base_url=gateway_url)
    elif backend == "bedrock":
        return AsyncAnthropicBedrock(
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            aws_region=aws_region,
        )
    elif backend == "ollama":
        return AsyncAnthropic(base_url=ollama_url, api_key="ollama")
    else:
        raise ValueError(f"Unknown backend: {backend!r}. Use 'gateway', 'bedrock', or 'ollama' (the Relay handles 'openai' itself).")


def create_sync_client(
    backend: str = "gateway",
    gateway_url: str = "",
    gateway_api_key: str = "",
    aws_access_key: str = "",
    aws_secret_key: str = "",
    aws_region: str = "us-east-1",
    ollama_url: str = "http://localhost:11434",
) -> Anthropic | AnthropicBedrock:
    """Create the appropriate sync Anthropic client for the chosen backend."""
    if backend == "gateway":
        return Anthropic(api_key=gateway_api_key, base_url=gateway_url)
    elif backend == "bedrock":
        return AnthropicBedrock(
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            aws_region=aws_region,
        )
    elif backend == "ollama":
        return Anthropic(base_url=ollama_url, api_key="ollama")
    else:
        raise ValueError(f"Unknown backend: {backend!r}. Use 'gateway', 'bedrock', or 'ollama' (the Relay handles 'openai' itself).")
