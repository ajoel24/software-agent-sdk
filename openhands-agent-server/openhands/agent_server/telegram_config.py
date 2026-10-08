"""Telegram bot configuration and non-secret prefs.

Secret material (bot token, webhook secret) lives in the SecretsStore —
the same store LLM credentials use — with env-var overrides. The prefs
file here holds non-secret bot prefs only and never token material.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID


TELEGRAM_BOT_TOKEN_SECRET_NAME = "telegram_bot_token"
TELEGRAM_WEBHOOK_SECRET_NAME = "telegram_webhook_secret"

TELEGRAM_SOURCE_TAG = "telegram"
TELEGRAM_CHAT_ID_TAG = "chatid"
FINISH_TOOL_NAME = "finish"
AGENT_SOURCE = "agent"


class TelegramChatStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


class TelegramServiceStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class TelegramStartResult(str, Enum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    STOPPED = "stopped"


class TelegramEnv:
    """Single location for every TELEGRAM_* environment read."""

    @property
    def bot_token(self) -> str:
        return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()

    @property
    def webhook_url(self) -> str | None:
        return os.environ.get("TELEGRAM_WEBHOOK_URL")

    @property
    def webhook_secret(self) -> str | None:
        return os.environ.get("TELEGRAM_WEBHOOK_SECRET")

    @property
    def allowed_usernames(self) -> str | None:
        return os.environ.get("TELEGRAM_ALLOWED_USERNAMES")

    @property
    def default_workspace(self) -> str | None:
        return os.environ.get("TELEGRAM_DEFAULT_WORKSPACE")

    @property
    def agent_profile(self) -> str | None:
        return os.environ.get("TELEGRAM_AGENT_PROFILE")

    @property
    def config_file(self) -> str | None:
        return os.environ.get("TELEGRAM_CONFIG_FILE")


@dataclass
class TelegramChatSession:
    """Tracks the link between a Telegram chat and an OpenHands conversation."""

    chat_id: int
    chat_title: str | None = None
    chat_username: str | None = None
    conversation_id: UUID | None = None
    profile_name: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    message_count: int = 0
    status: str = "idle"


@dataclass
class TelegramConfig:
    """Configuration for the Telegram bot."""

    bot_token: str
    webhook_url: str | None = None
    webhook_secret: str | None = None
    allowed_usernames: list[str] = field(default_factory=list)
    default_workspace: str = "/workspace"
    agent_profile_name: str = "default"
    max_concurrent_chats: int = 10


def _telegram_prefs_path() -> Path:
    """Location of the non-secret bot prefs (never holds token material)."""
    override = TelegramEnv().config_file
    if override:
        return Path(override)
    return Path.home() / ".openhands" / "telegram.json"


def save_telegram_prefs(config: TelegramConfig) -> None:
    """Persist non-secret bot prefs. Secrets go to the SecretsStore."""
    path = _telegram_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "webhook_url": config.webhook_url,
        "allowed_usernames": config.allowed_usernames,
        "default_workspace": config.default_workspace,
        "agent_profile_name": config.agent_profile_name,
    }
    path.write_text(json.dumps(payload, indent=2))


def load_telegram_prefs() -> dict[str, Any]:
    """Load persisted non-secret bot prefs, if any."""
    try:
        data = json.loads(_telegram_prefs_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
