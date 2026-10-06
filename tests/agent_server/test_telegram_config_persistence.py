"""Tests for Telegram secret handling via the SecretsStore."""

import json

import pytest

from openhands.agent_server.persistence.store import FileSecretsStore
from openhands.agent_server.telegram_service import (
    TelegramConfig,
    _set_telegram_service,
    load_telegram_prefs,
    save_telegram_prefs,
)


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    path = tmp_path / "telegram.json"
    monkeypatch.setenv("TELEGRAM_CONFIG_FILE", str(path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    _set_telegram_service(None)
    yield path
    _set_telegram_service(None)


@pytest.fixture
def store(tmp_path):
    return FileSecretsStore(persistence_dir=tmp_path / "persist")


def test_prefs_hold_no_secrets(prefs_file):
    save_telegram_prefs(
        TelegramConfig(
            bot_token="123:ABC",
            allowed_usernames=["alice"],
            default_workspace="/projects",
        )
    )
    saved = json.loads(prefs_file.read_text())
    assert "bot_token" not in saved
    assert saved["allowed_usernames"] == ["alice"]

    prefs = load_telegram_prefs()
    assert prefs["default_workspace"] == "/projects"


def test_token_roundtrip_in_store(store):
    store.set_secret("telegram_bot_token", "  123:ABC\t", "Telegram bot token")
    assert store.get_secret("telegram_bot_token") == "  123:ABC\t"


def test_no_prefs_file(prefs_file):
    assert load_telegram_prefs() == {}


def test_env_priority_over_store(prefs_file, store, monkeypatch):
    from unittest.mock import MagicMock, patch

    from openhands.agent_server.telegram_router import _config_from_env_or_store

    store.set_secret("telegram_bot_token", "store-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  env-token\t")

    with (
        patch(
            "openhands.agent_server.telegram_router.get_secrets_store",
            return_value=store,
        ),
        patch(
            "openhands.agent_server.telegram_router.get_config",
            return_value=MagicMock(),
        ),
    ):
        config = _config_from_env_or_store(MagicMock())
    assert config is not None
    assert config.bot_token == "env-token"


def test_store_fallback_without_env(prefs_file, store, monkeypatch):
    from unittest.mock import MagicMock, patch

    from openhands.agent_server.telegram_router import _config_from_env_or_store

    store.set_secret("telegram_bot_token", "store-token")
    save_telegram_prefs(TelegramConfig(bot_token="store-token"))

    with (
        patch(
            "openhands.agent_server.telegram_router.get_secrets_store",
            return_value=store,
        ),
        patch(
            "openhands.agent_server.telegram_router.get_config",
            return_value=MagicMock(),
        ),
    ):
        config = _config_from_env_or_store(MagicMock())
    assert config is not None
    assert config.bot_token == "store-token"


def test_nothing_configured(prefs_file, store, monkeypatch):
    from unittest.mock import MagicMock, patch

    from openhands.agent_server.telegram_router import _config_from_env_or_store

    with (
        patch(
            "openhands.agent_server.telegram_router.get_secrets_store",
            return_value=store,
        ),
        patch(
            "openhands.agent_server.telegram_router.get_config",
            return_value=MagicMock(),
        ),
    ):
        assert _config_from_env_or_store(MagicMock()) is None
