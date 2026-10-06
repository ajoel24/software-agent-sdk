"""Tests for the OpenCode Zen gateway provider."""

from openhands.sdk.llm import LLM
from openhands.sdk.llm.utils.opencode_provider import (
    OPENCODE_API_KEY_ENV_VAR,
    OPENCODE_ZEN_ANTHROPIC_BASE_URL,
    OPENCODE_ZEN_BASE_URL,
    is_opencode_model,
    to_litellm_model,
    zen_api_for,
    zen_base_url,
)
from openhands.sdk.llm.utils.openhands_provider import litellm_call_kwargs
from openhands.sdk.llm.utils.unverified_models import _extract_model_and_provider
from openhands.sdk.llm.utils.verified_models import VERIFIED_MODELS


def test_zen_api_routing_matches_pi_convention():
    assert zen_api_for("opencode/muse-spark-1.3") == "responses"
    assert zen_api_for("opencode/gpt-6-astra") == "responses"
    assert zen_api_for("opencode/claude-opus-5") == "messages"
    assert zen_api_for("opencode/deepseek-v4-pro") == "chat"
    assert zen_api_for("opencode/some-future-model") == "chat"


def test_chat_models_translate_to_openai_form():
    kwargs = litellm_call_kwargs("opencode/deepseek-v4-pro", None)
    assert kwargs == {
        "model": "openai/deepseek-v4-pro",
        "api_base": OPENCODE_ZEN_BASE_URL,
    }


def test_messages_models_translate_to_anthropic_form():
    kwargs = litellm_call_kwargs("opencode/claude-opus-5", None)
    assert kwargs == {
        "model": "anthropic/claude-opus-5",
        # No /v1 suffix: LiteLLM appends /v1/messages itself.
        "api_base": OPENCODE_ZEN_ANTHROPIC_BASE_URL,
    }
    assert not kwargs["api_base"].endswith("/v1")


def test_responses_models_use_openai_form_on_zen_base():
    kwargs = litellm_call_kwargs("opencode/muse-spark-1.3", None)
    assert kwargs == {
        "model": "openai/muse-spark-1.3",
        "api_base": OPENCODE_ZEN_BASE_URL,
    }


def test_opencode_explicit_base_url_wins():
    kwargs = litellm_call_kwargs(
        "opencode/claude-opus-5", "https://proxy.example.com/v1"
    )
    assert kwargs == {
        "model": "anthropic/claude-opus-5",
        "api_base": "https://proxy.example.com/v1",
    }
    assert zen_base_url("opencode/claude-opus-5", None) == (
        OPENCODE_ZEN_ANTHROPIC_BASE_URL
    )


def test_non_opencode_models_pass_through():
    assert litellm_call_kwargs("openai/gpt-6-astra", None) == {
        "model": "openai/gpt-6-astra",
        "api_base": None,
    }
    assert litellm_call_kwargs("anthropic/claude-opus-5", None) == {
        "model": "anthropic/claude-opus-5",
        "api_base": None,
    }


def test_opencode_prefix_extracts_as_provider():
    assert _extract_model_and_provider("opencode/gpt-6-astra") == (
        "opencode",
        "gpt-6-astra",
        "/",
    )


def test_is_opencode_model_rejects_non_strings():
    assert is_opencode_model("opencode/gpt-6-astra")
    assert not is_opencode_model("openai/gpt-6-astra")
    assert not is_opencode_model(None)


def test_to_litellm_model_routes_by_protocol():
    assert to_litellm_model("opencode/muse-spark-1.3") == "openai/muse-spark-1.3"
    assert to_litellm_model("opencode/claude-opus-5") == "anthropic/claude-opus-5"
    assert to_litellm_model("opencode/deepseek-v4-pro") == "openai/deepseek-v4-pro"


def test_llm_responses_models_default_to_responses_api():
    llm = LLM(model="opencode/muse-spark-1.3")
    assert llm.model == "opencode/muse-spark-1.3"
    assert llm.api_mode == "responses"
    assert llm.uses_responses_api()


def test_llm_messages_and_chat_models_stay_on_chat_api():
    assert LLM(model="opencode/claude-opus-5").api_mode == "auto"
    assert LLM(model="opencode/deepseek-v4-pro").api_mode == "auto"
    assert not LLM(model="opencode/claude-opus-5").uses_responses_api()


def test_llm_explicit_api_mode_wins():
    llm = LLM(model="opencode/muse-spark-1.3", api_mode="chat")
    assert llm.api_mode == "chat"
    assert not llm.uses_responses_api()


def test_llm_defaults_api_key_from_environment(monkeypatch):
    monkeypatch.setenv(OPENCODE_API_KEY_ENV_VAR, "zen-key")
    llm = LLM(model="opencode/gpt-6-astra")
    assert llm.model == "opencode/gpt-6-astra"
    assert llm.api_key is not None
    assert llm.api_key.get_secret_value() == "zen-key"


def test_llm_explicit_api_key_wins_over_environment(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setenv(OPENCODE_API_KEY_ENV_VAR, "zen-key")
    llm = LLM(model="opencode/gpt-6-astra", api_key=SecretStr("explicit"))
    assert llm.api_key is not None
    assert llm.api_key.get_secret_value() == "explicit"


def test_llm_without_env_key_has_no_key(monkeypatch):
    monkeypatch.delenv(OPENCODE_API_KEY_ENV_VAR, raising=False)
    llm = LLM(model="opencode/gpt-6-astra")
    assert llm.api_key is None


def test_meta_muse_models_are_verified():
    assert VERIFIED_MODELS["meta"] == [
        "muse-spark-1.3",
        "muse-spark-1.3-contributor",
        "muse-spark-1.2",
        "muse-spark-1.2-contributor",
    ]


def test_opencode_is_a_verified_provider():
    assert VERIFIED_MODELS["opencode"]
    assert not any(m.startswith("opencode/") for m in VERIFIED_MODELS["opencode"])
    assert "muse-spark-1.3" in VERIFIED_MODELS["opencode"]
