"""OpenCode Zen gateway provider.

Zen (https://opencode.ai/docs/zen) is an OpenAI-compatible model gateway,
similar to OpenRouter: models are addressed as ``opencode/<model-id>`` and
authenticated with ``OPENCODE_API_KEY``.

Unlike OpenRouter, Zen serves each model on exactly one protocol, the same
per-model ``api`` routing the pi agent uses (``anthropic-messages``,
``openai-completions`` or ``openai-responses``; see ``opencodeProvider``
in pi-mono):

- ``/chat/completions`` models (default) translate to the ``openai/<id>``
  form against ``https://opencode.ai/zen/v1``.
- ``/messages`` models (Claude, Qwen) translate to the ``anthropic/<id>``
  form against ``https://opencode.ai/zen`` — LiteLLM appends
  ``/v1/messages`` itself, so the base must not carry ``/v1``.
- ``/responses`` models (GPT, Grok, Muse Spark) translate to the
  ``openai/<id>`` form against ``https://opencode.ai/zen/v1`` and run
  through the SDK's Responses-API path (``api_mode="responses"``).

LiteLLM has no native ``opencode`` provider, so this module owns the
translation. The canonical ``opencode/<id>`` name is preserved everywhere
else (events, persistence, UI); only the LiteLLM call boundary and the
``api_mode`` default see the translated form.

The protocol sets below follow the Zen docs model table. Refresh them when
Zen adds models (re-scrape https://opencode.ai/docs/zen/): unknown ids
default to chat completions.
"""

from __future__ import annotations

from typing import Final, Literal


OPENCODE_MODEL_PREFIX: Final[str] = "opencode/"
OPENCODE_ZEN_BASE_URL: Final[str] = "https://opencode.ai/zen/v1"
OPENCODE_ZEN_ANTHROPIC_BASE_URL: Final[str] = "https://opencode.ai/zen"
OPENCODE_API_KEY_ENV_VAR: Final[str] = "OPENCODE_API_KEY"

ZenApi = Literal["chat", "messages", "responses"]

# Models served on the Anthropic Messages protocol (Zen docs ``/messages``).
OPENCODE_MESSAGES_MODELS: Final[frozenset[str]] = frozenset(
    {
        "claude-fable-5",
        "claude-fable-5-1",
        "claude-haiku-4-5",
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "qwen3.5-plus",
        "qwen3.6-plus",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.8-flash",
    }
)

# Models served on the OpenAI Responses protocol (Zen docs ``/responses``).
OPENCODE_RESPONSES_MODELS: Final[frozenset[str]] = frozenset(
    {
        "gpt-5",
        "gpt-5-codex",
        "gpt-5-nano",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5.2",
        "gpt-5.2-codex",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.4-pro",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-6-astra",
        "gpt-6-luna",
        "gpt-6-sol",
        "gpt-6.1-sol",
        "grok-4.5",
        "grok-4.6",
        "grok-4.7",
        "grok-build-0.1",
        "muse-spark-1.2",
        "muse-spark-1.2-contributor",
        "muse-spark-1.3",
        "muse-spark-1.3-contributor",
        "muse-spark-1.3-contributor-free",
    }
)


def is_opencode_model(model: str | None) -> bool:
    """Whether a model id routes through the OpenCode Zen gateway."""
    return isinstance(model, str) and model.startswith(OPENCODE_MODEL_PREFIX)


def _zen_model_id(model: str) -> str:
    return model[len(OPENCODE_MODEL_PREFIX) :]


def zen_api_for(model: str) -> ZenApi:
    """Return the Zen protocol serving an ``opencode/<id>`` model."""
    model_id = _zen_model_id(model)
    if model_id in OPENCODE_MESSAGES_MODELS:
        return "messages"
    if model_id in OPENCODE_RESPONSES_MODELS:
        return "responses"
    return "chat"


def to_litellm_model(model: str) -> str:
    """Translate ``opencode/<id>`` to the LiteLLM-routable provider form."""
    model_id = _zen_model_id(model)
    if zen_api_for(model) == "messages":
        return f"anthropic/{model_id}"
    return f"openai/{model_id}"


def zen_base_url(model: str, explicit: str | None) -> str | None:
    """Resolve the Zen base URL, honoring an explicit override."""
    if explicit:
        return explicit
    if zen_api_for(model) == "messages":
        return OPENCODE_ZEN_ANTHROPIC_BASE_URL
    return OPENCODE_ZEN_BASE_URL
