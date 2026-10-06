"""Tests for the OpenCode Zen gateway provider."""

from openhands.sdk.llm import LLM
from openhands.sdk.llm.utils.opencode_provider import (
    OPENCODE_API_KEY_ENV_VAR,
    OPENCODE_ZEN_BASE_URL,
    is_opencode_model,
    to_litellm_model,
)
from openhands.sdk.llm.utils.openhands_provider import litellm_call_kwargs
from openhands.sdk.llm.utils.unverified_models import _extract_model_and_provider
from openhands.sdk.llm.utils.verified_models import VERIFIED_MODELS


def test_opencode_models_translate_to_openai_form():
    kwargs = litellm_call_kwargs("opencode/gpt-6-astra", None)
    assert kwargs == {
        "model": "openai/gpt-6-astra",
        "api_base": OPENCODE_ZEN_BASE_URL,
    }


def test_opencode_explicit_base_url_wins():
    kwargs = litellm_call_kwargs("opencode/gpt-6-astra", "https://proxy.example.com/v1")
    assert kwargs == {
        "model": "openai/gpt-6-astra",
        "api_base": "https://proxy.example.com/v1",
    }


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


def test_to_litellm_model_strips_prefix():
    assert to_litellm_model("opencode/muse-spark-1.3") == "openai/muse-spark-1.3"


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
