"""OpenCode Zen gateway provider.

Zen (https://opencode.ai/docs/zen) is an OpenAI-compatible model gateway,
similar to OpenRouter: models are addressed as ``opencode/<model-id>`` and
served from ``https://opencode.ai/zen/v1`` with ``OPENCODE_API_KEY``.

LiteLLM has no native ``opencode`` provider, so this module owns the
translation to the ``openai/`` form LiteLLM routes, mirroring how
``openhands_provider`` translates the ``openhands/`` prefix. The canonical
``opencode/<id>`` name is preserved everywhere else (events, persistence,
UI); only the LiteLLM call boundary sees the translated form.
"""

from __future__ import annotations


OPENCODE_MODEL_PREFIX = "opencode/"
OPENCODE_ZEN_BASE_URL = "https://opencode.ai/zen/v1"
OPENCODE_API_KEY_ENV_VAR = "OPENCODE_API_KEY"


def is_opencode_model(model: str | None) -> bool:
    """Whether a model id routes through the OpenCode Zen gateway."""
    return isinstance(model, str) and model.startswith(OPENCODE_MODEL_PREFIX)


def to_litellm_model(model: str) -> str:
    """Translate ``opencode/<id>`` to the ``openai/<id>`` form LiteLLM routes."""
    return "openai/" + model[len(OPENCODE_MODEL_PREFIX) :]
