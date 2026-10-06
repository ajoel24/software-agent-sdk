"""Tests for Telegram bot token persistence."""

import json
import os

import pytest

from openhands.agent_server.telegram_service import (
    TelegramConfig,
    _set_telegram_service,
    load_telegram_config,
    save_telegram_config,
)


@pytest.fixture
def isolated_config_file(tmp_path, monkeypatch):
    path = tmp_path / "telegram.json"
    monkeypatch.setenv("TELEGRAM_CONFIG_FILE", str(path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    _set_telegram_service(None)
    yield path
    _set_telegram_service(None)


def test_roundtrip(isolated_config_file):
    config = TelegramConfig(
        bot_token="  123:ABC\t",
        webhook_url=None,
        webhook_secret=None,
        allowed_usernames=["alice"],
        default_workspace="/projects",
    )
    save_telegram_config(config)

    loaded = load_telegram_config()
    assert loaded is not None
    assert loaded.bot_token == "123:ABC"
    assert loaded.allowed_usernames == ["alice"]
    assert loaded.default_workspace == "/projects"


def test_missing_file_returns_none(isolated_config_file):
    assert load_telegram_config() is None


def test_blank_token_not_loaded(isolated_config_file):
    isolated_config_file.write_text(json.dumps({"bot_token": "  \t "}))
    assert load_telegram_config() is None


def test_file_mode_is_private(isolated_config_file):
    save_telegram_config(TelegramConfig(bot_token="123:ABC"))
    assert (isolated_config_file.stat().st_mode & 0o777) == 0o600


def test_env_takes_priority(isolated_config_file, monkeypatch):
    save_telegram_config(TelegramConfig(bot_token="file-token"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  env-token\t")

    from openhands.agent_server.telegram_router import _config_from_env_or_file

    config = _config_from_env_or_file()
    assert config is not None
    assert config.bot_token == "env-token"


def test_file_fallback_without_env(isolated_config_file):
    save_telegram_config(TelegramConfig(bot_token="file-token"))

    from openhands.agent_server.telegram_router import _config_from_env_or_file

    config = _config_from_env_or_file()
    assert config is not None
    assert config.bot_token == "file-token"


def test_no_config_anywhere(isolated_config_file):
    from openhands.agent_server.telegram_router import _config_from_env_or_file

    assert _config_from_env_or_file() is None
    assert "TELEGRAM_BOT_TOKEN" not in os.environ
