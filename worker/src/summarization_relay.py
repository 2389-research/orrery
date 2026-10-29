# ABOUTME: Builds the optional dedicated relay used for summarization (e.g. LunaRoute).
# ABOUTME: Returns None when unconfigured or misconfigured, meaning callers use the local relay.

import logging

from orrery_relay import Relay

logger = logging.getLogger("summarization_relay")


def build_summarization_relay(settings) -> Relay | None:
    if not settings.summarization_backend:
        return None
    try:
        return Relay(
            backend=settings.summarization_backend,
            gateway_url=settings.summarization_gateway_url,
            gateway_api_key=settings.summarization_gateway_api_key,
            ollama_url=settings.ollama_url,
            reasoning_effort=settings.summarization_reasoning_effort,
            timeout=settings.summarization_timeout_seconds,
        )
    except Exception as e:
        # A typo'd SUMMARIZATION_BACKEND must not crash every summarization job: fall back
        # to local (the resolver warns per remote-opted workspace) and say why, once here.
        logger.warning("summarization relay disabled — could not build backend %r: %s",
                       settings.summarization_backend, e)
        return None
