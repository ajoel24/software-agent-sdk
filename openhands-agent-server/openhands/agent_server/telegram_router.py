"""Telegram router for OpenHands Agent Server.

This module defines the HTTP API endpoints for Telegram bot operations.
Business logic is delegated to telegram_service.py.
"""

from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from openhands.agent_server._secrets_exposure import get_config
from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.dependencies import get_conversation_service
from openhands.agent_server.persistence import SecretsStore, get_secrets_store
from openhands.agent_server.telegram_config import (
    TELEGRAM_BOT_TOKEN_SECRET_NAME,
    TELEGRAM_WEBHOOK_SECRET_NAME,
    TelegramChatStatus,
    TelegramConfig,
    TelegramEnv,
    TelegramServiceStatus,
    TelegramStartResult,
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
    status: str = TelegramChatStatus.IDLE.value
    message_count: int = 0
    last_activity: str | None = None


class TelegramStatusResponse(BaseModel):
    status: str
    active_chats: int
    total_messages: int
    bot_token_configured: bool
    webhook_url: str | None = None


class TelegramStartResponse(TelegramStatusResponse):
    result: TelegramStartResult


def _parse_allowed_usernames(raw: str | None) -> list[str]:
    """Parse a comma-separated username allowlist, tolerating @-prefixes."""
    return [u.strip().lstrip("@") for u in (raw or "").split(",") if u.strip()]


def _config_from_env_or_store(
    request: Request, req: TelegramStartRequest | None = None
) -> TelegramConfig | None:
    """Bot config: explicit request fields, then env, then stored values.

    Token material lives in the SecretsStore (the same store LLM
    credentials use); the prefs file under ~/.openhands holds non-secret
    prefs only.
    """
    env = TelegramEnv()
    store = get_secrets_store(get_config(request))
    token = ((req.bot_token if req else None) or env.bot_token).strip()
    if not token:
        stored = store.get_secret(TELEGRAM_BOT_TOKEN_SECRET_NAME)
        token = (stored or "").strip()
    if not token:
        return None
    prefs = load_telegram_prefs()
    return TelegramConfig(
        bot_token=token,
        webhook_url=(req.webhook_url if req else None)
        or env.webhook_url
        or prefs.get("webhook_url"),
        webhook_secret=env.webhook_secret
        or store.get_secret(TELEGRAM_WEBHOOK_SECRET_NAME),
        allowed_usernames=(req.allowed_usernames if req else None)
        or _parse_allowed_usernames(env.allowed_usernames)
        or list(prefs.get("allowed_usernames") or []),
        default_workspace=(req.default_workspace if req else None)
        or env.default_workspace
        or str(prefs.get("default_workspace") or "/workspace"),
        agent_profile_name=(req.agent_profile_name if req else None)
        or env.agent_profile
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
    secret = service.config.webhook_secret
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )
    payload = await request.json()
    await service.handle_webhook(payload)
    return TelegramWebhookResponse()


@telegram_router.get("/status", response_model=TelegramStatusResponse)
async def telegram_status(
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
) -> TelegramStatusResponse:
    """Get the current status of the Telegram bot integration."""
    live = service.get_status()
    return TelegramStatusResponse(
        status=live["status"],
        active_chats=live["active_chats"],
        total_messages=live["total_messages"],
        bot_token_configured=bool(service.config.bot_token),
        webhook_url=service.config.webhook_url,
    )


def _resolve_start_config(
    req: TelegramStartRequest, request: Request
) -> TelegramConfig | None:
    return _config_from_env_or_store(request, req)


def _start_response(
    service: TelegramBotService, result: TelegramStartResult
) -> TelegramStartResponse:
    live = service.get_status()
    return TelegramStartResponse(
        result=result,
        status=live["status"],
        active_chats=live["active_chats"],
        total_messages=live["total_messages"],
        bot_token_configured=bool(service.config.bot_token),
        webhook_url=service.config.webhook_url,
    )


def _persist_start_config(store: SecretsStore, config: TelegramConfig) -> None:
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


@telegram_router.post("/start", response_model=TelegramStartResponse)
async def telegram_start(
    req: TelegramStartRequest,
    request: Request,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> TelegramStartResponse:
    """Start the Telegram bot.

    If the bot is already running, returns its current status.
    On success the token is stored in the SecretsStore, so restarts
    don't need re-entry.
    """
    existing = _get_telegram_service_state(request)
    if existing and existing.is_running():
        return _start_response(existing, TelegramStartResult.ALREADY_RUNNING)
    if existing:
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
    return _start_response(service, TelegramStartResult.STARTED)


@telegram_router.post("/stop")
async def telegram_stop(
    request: Request,
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
) -> dict:
    """Stop the Telegram bot."""
    await service.stop()
    _set_telegram_service_state(request, None)
    return {"status": TelegramServiceStatus.STOPPED.value}


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
