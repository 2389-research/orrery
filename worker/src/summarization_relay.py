# ABOUTME: Builds the optional dedicated relay used for summarization (e.g. LunaRoute).
# ABOUTME: Returns None when unconfigured, meaning callers use the local relay.

from orrery_relay import Relay


def build_summarization_relay(settings) -> Relay | None:
    if not settings.summarization_backend:
        return None
    return Relay(
        backend=settings.summarization_backend,
        gateway_url=settings.summarization_gateway_url,
        gateway_api_key=settings.summarization_gateway_api_key,
        ollama_url=settings.ollama_url,
    )
