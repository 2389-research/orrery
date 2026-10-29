# ABOUTME: Tests for backend client factory and model ID mapping.
# ABOUTME: Verifies gateway/bedrock client creation and model name translation.

from unittest.mock import patch
import pytest


def test_create_async_client_gateway():
    from orrery_relay.backends import create_async_client
    with patch("orrery_relay.backends.AsyncAnthropic") as mock_cls:
        create_async_client(
            backend="gateway",
            gateway_url="https://gw.example.com",
            gateway_api_key="test-key",
        )
        mock_cls.assert_called_once_with(
            api_key="test-key",
            base_url="https://gw.example.com",
        )


def test_create_async_client_bedrock():
    from orrery_relay.backends import create_async_client
    with patch("orrery_relay.backends.AsyncAnthropicBedrock") as mock_cls:
        create_async_client(
            backend="bedrock",
            aws_access_key="ak",
            aws_secret_key="sk",
            aws_region="us-west-2",
        )
        mock_cls.assert_called_once_with(
            aws_access_key="ak",
            aws_secret_key="sk",
            aws_region="us-west-2",
        )


def test_create_sync_client_gateway():
    from orrery_relay.backends import create_sync_client
    with patch("orrery_relay.backends.Anthropic") as mock_cls:
        create_sync_client(
            backend="gateway",
            gateway_url="https://gw.example.com",
            gateway_api_key="test-key",
        )
        mock_cls.assert_called_once_with(
            api_key="test-key",
            base_url="https://gw.example.com",
        )


def test_create_sync_client_bedrock():
    from orrery_relay.backends import create_sync_client
    with patch("orrery_relay.backends.AnthropicBedrock") as mock_cls:
        create_sync_client(
            backend="bedrock",
            aws_access_key="ak",
            aws_secret_key="sk",
            aws_region="us-east-1",
        )
        mock_cls.assert_called_once_with(
            aws_access_key="ak",
            aws_secret_key="sk",
            aws_region="us-east-1",
        )


def test_map_model_gateway_passes_through():
    from orrery_relay.backends import map_model_id
    assert map_model_id("claude-sonnet-4-6", "gateway") == "claude-sonnet-4-6"
    assert map_model_id("claude-haiku-4-5", "gateway") == "claude-haiku-4-5"


def test_map_model_bedrock_translates():
    from orrery_relay.backends import BEDROCK_MODEL_MAP, map_model_id
    # Literal id catches a wrong map entry; the map cross-check catches drift.
    assert map_model_id("claude-sonnet-4-6", "bedrock") == "us.anthropic.claude-sonnet-4-6"
    assert map_model_id("claude-sonnet-4-6", "bedrock") == BEDROCK_MODEL_MAP["claude-sonnet-4-6"]
    assert map_model_id("claude-haiku-4-5", "bedrock") == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert map_model_id("claude-opus-4-6", "bedrock") == "us.anthropic.claude-opus-4-6-v1"


def test_map_model_bedrock_passthrough_for_already_bedrock_ids():
    from orrery_relay.backends import map_model_id
    bedrock_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    assert map_model_id(bedrock_id, "bedrock") == bedrock_id


def test_create_async_client_raises_on_unknown_backend():
    from orrery_relay.backends import create_async_client
    with pytest.raises(ValueError, match="Unknown backend"):
        create_async_client(backend="unknown")
