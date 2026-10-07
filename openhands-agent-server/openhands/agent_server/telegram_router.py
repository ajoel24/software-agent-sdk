"""Telegram router for OpenHands Agent Server.

This module defines the HTTP API endpoints for Telegram bot operations.
Business logic is delegated to telegram_service.py.
"""

import os
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from openhands.agent_server._secrets_exposure import get_config
from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.dependencies import get_conversation_service
from openhands.agent_server.persistence import get_secrets_store
from openhands.agent_server.telegram_config import (
    TELEGRAM_BOT_TOKEN_SECRET_NAME,
    TELEGRAM_WEBHOOK_SECRET_NAME,
    TelegramConfig,
    load_telegram_prefs,
    save_telegram_prefs,
)
from openhands.agent_server.telegram_service import TelegramBotService
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

telegram_router = APIRouter(prefix="/telegram", tags=["Telegram"])


class TelegramStartRequest(BaseModel):
    """Request body for starting the Telegram bot."""

    bot_token: str | None = Field(
        default=None,
        description="Telegram Bot API token from @BotFather",
    )
    webhook_url: str | None = Field(
        default=None,
        description="Webhook URL for Telegram updates. If None, uses polling.",
    )
    allowed_usernames: list[str] | None = Field(
        default=None,
        description="Allowed Telegram usernames. Empty allows all.",
    )
    default_workspace: str = Field(
        default="/workspace",
        description="Default workspace for Telegram-triggered conversations",
    )
    agent_profile_name: str | None = Field(
        default=None,
        description="Agent profile for Telegram conversations. "
        "Defaults to the server's default profile.",
    )


class TelegramWebhookResponse(BaseModel):
    """Response for webhook updates."""

    ok: bool = True


class TelegramChatSessionResponse(BaseModel):
    """One active Telegram chat session; nulls mean 'not linked yet'."""

    chat_id: int
    chat_title: str | None = None
    chat_username: str | None = None
    conversation_id: str | None = None
    status: str = "idle"
    message_count: int = 0
    last_activity: str | None = None


def _parse_allowed_usernames(raw: str | None) -> list[str]:
    """Parse a comma-separated username allowlist, tolerating @-prefixes."""
    return [u.strip().lstrip("@") for u in (raw or "").split(",") if u.strip()]


def _config_from_env_or_store(request: Request) -> TelegramConfig | None:
    """Bot config from env, falling back to persisted prefs + secret.

    Secret handling mirrors the LLM-settings pattern: token material lives
    in the SecretsStore (same store LLM credentials use) with an env-var
    override; the prefs file under ~/.openhands holds non-secret prefs only.
    """
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    store = get_secrets_store(get_config(request))
    if not token:
        stored = store.get_secret(TELEGRAM_BOT_TOKEN_SECRET_NAME)
        token = (stored or "").strip()
    if not token:
        return None
    prefs = load_telegram_prefs()
    return TelegramConfig(
        bot_token=token,
        webhook_url=os.environ.get("TELEGRAM_WEBHOOK_URL") or prefs.get("webhook_url"),
        webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        or store.get_secret(TELEGRAM_WEBHOOK_SECRET_NAME),
        allowed_usernames=_parse_allowed_usernames(
            os.environ.get("TELEGRAM_ALLOWED_USERNAMES")
        )
        or list(prefs.get("allowed_usernames") or []),
        default_workspace=os.environ.get("TELEGRAM_DEFAULT_WORKSPACE")
        or str(prefs.get("default_workspace") or "/workspace"),
        agent_profile_name=os.environ.get("TELEGRAM_AGENT_PROFILE")
        or str(prefs.get("agent_profile_name") or "default"),
    )


def _get_telegram_service_state(request: Request) -> TelegramBotService | None:
    """The singleton bot service, owned by the app lifespan (app.state)."""
    return getattr(request.app.state, "telegram_service", None)


def _set_telegram_service_state(
    request: Request, service: TelegramBotService | None
) -> None:
    request.app.state.telegram_service = service


def _get_or_create_telegram_service(
    request: Request,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> TelegramBotService:
    service = _get_telegram_service_state(request)
    if service is not None:
        return service
    config = _config_from_env_or_store(request)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Telegram bot isn't set up yet. Start it from the Telegram "
                "page (paste the token from @BotFather) or set "
                "TELEGRAM_BOT_TOKEN and POST /telegram/start."
            ),
        )
    service = TelegramBotService(config, conversation_service)
    _set_telegram_service_state(request, service)
    return service


@telegram_router.post("/webhook", response_model=TelegramWebhookResponse)
async def telegram_webhook(
    request: Request,
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
) -> TelegramWebhookResponse:
    """Receive webhook updates from Telegram.

    Set this URL in your Telegram bot webhook configuration.
    When the bot runs in polling mode, this endpoint is not used.
    """
    if not service.is_running():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram bot is not running",
        )
    payload = await request.json()
    await service.handle_webhook(payload)
    return TelegramWebhookResponse()


@telegram_router.get("/status")
async def telegram_status(
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
) -> dict:
    """Get the current status of the Telegram bot integration."""
    result = service.get_status()
    result["bot_token_configured"] = bool(service.config.bot_token)
    result["webhook_url"] = service.config.webhook_url
    return result


def _resolve_start_config(
    req: TelegramStartRequest, request: Request
) -> TelegramConfig | None:
    """Build the bot config for an explicit start request, if possible."""
    token = (req.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if token:
        return TelegramConfig(
            bot_token=token,
            webhook_url=req.webhook_url or os.environ.get("TELEGRAM_WEBHOOK_URL"),
            webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
            allowed_usernames=req.allowed_usernames
            or _parse_allowed_usernames(os.environ.get("TELEGRAM_ALLOWED_USERNAMES")),
            default_workspace=req.default_workspace,
            agent_profile_name=req.agent_profile_name
            or os.environ.get("TELEGRAM_AGENT_PROFILE")
            or "default",
        )
    return _config_from_env_or_store(request)


def _persist_start_config(store, config: TelegramConfig) -> None:
    """Store token material in the SecretsStore and prefs on disk."""
    try:
        store.set_secret(
            TELEGRAM_BOT_TOKEN_SECRET_NAME,
            config.bot_token,
            "Telegram bot token",
        )
        if config.webhook_secret:
            store.set_secret(
                TELEGRAM_WEBHOOK_SECRET_NAME,
                config.webhook_secret,
                "Telegram webhook secret",
            )
        save_telegram_prefs(config)
    except OSError as exc:
        logger.warning(f"Could not persist Telegram config: {exc}")


@telegram_router.post("/start")
async def telegram_start(
    req: TelegramStartRequest,
    request: Request,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> dict:
    """Start the Telegram bot.

    If the bot is already running, returns its current status.
    On success the token is stored in the SecretsStore, so restarts
    don't need re-entry.
    """
    existing = _get_telegram_service_state(request)
    if existing and existing.is_running():
        return {"status": "already_running", **existing.get_status()}
    if existing:
        # A previous instance exists but isn't running (e.g. polling died
        # during start). Shut it down so its updater doesn't leak and spam
        # polling errors, then replace it below.
        with suppress(Exception):
            await existing.stop()
        _set_telegram_service_state(request, None)

    config = _resolve_start_config(req, request)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "I need a bot token to start. Paste the token from "
                "@BotFather into the Telegram page, or set "
                "TELEGRAM_BOT_TOKEN and try again."
            ),
        )
    service = TelegramBotService(config, conversation_service)
    _set_telegram_service_state(request, service)
    await service.start()
    _persist_start_config(get_secrets_store(get_config(request)), config)
    return {"status": "started", **service.get_status()}


@telegram_router.post("/stop")
async def telegram_stop(
    request: Request,
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
) -> dict:
    """Stop the Telegram bot."""
    await service.stop()
    _set_telegram_service_state(request, None)
    return {"status": "stopped"}


@telegram_router.get("/chats", response_model=list[TelegramChatSessionResponse])
async def telegram_chats(
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
    limit: int = 100,
    offset: int = 0,
) -> list[TelegramChatSessionResponse]:
    """List active Telegram chat sessions (paginated)."""
    sessions = list(service.get_chat_sessions())
    return [
        TelegramChatSessionResponse(
            chat_id=s.chat_id,
            chat_title=s.chat_title,
            chat_username=s.chat_username,
            conversation_id=str(s.conversation_id) if s.conversation_id else None,
            status=s.status,
            message_count=s.message_count,
            last_activity=s.last_activity.isoformat() if s.last_activity else None,
        )
        for s in sessions[max(offset, 0) : max(offset, 0) + max(limit, 0)]
    ]
