"""Telegram router for OpenHands Agent Server.

This module defines the HTTP API endpoints for Telegram bot operations.
Business logic is delegated to telegram_service.py.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.dependencies import get_conversation_service
from openhands.agent_server.telegram_service import (
    TelegramBotService,
    TelegramConfig,
    _get_telegram_service,
    _set_telegram_service,
)
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


class TelegramWebhookResponse(BaseModel):
    """Response for webhook updates."""

    ok: bool = True


def _get_or_create_telegram_service(
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> TelegramBotService:
    service = _get_telegram_service()
    if service is not None:
        return service
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Telegram bot not configured. "
                "Set TELEGRAM_BOT_TOKEN or POST /telegram/start"
            ),
        )
    config = TelegramConfig(
        bot_token=token,
        webhook_url=os.environ.get("TELEGRAM_WEBHOOK_URL"),
        webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
        allowed_usernames=[
            u.strip().lstrip("@")
            for u in os.environ.get("TELEGRAM_ALLOWED_USERNAMES", "").split(",")
            if u.strip()
        ],
        default_workspace=os.environ.get("TELEGRAM_DEFAULT_WORKSPACE", "/workspace"),
    )
    service = TelegramBotService(config, conversation_service)
    _set_telegram_service(service)
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


@telegram_router.post("/start")
async def telegram_start(
    req: TelegramStartRequest,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> dict:
    """Start the Telegram bot.

    If the bot is already running, returns its current status.
    """
    existing = _get_telegram_service()
    if existing and existing.is_running():
        return {"status": "already_running", **existing.get_status()}

    token = req.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bot_token is required (or set TELEGRAM_BOT_TOKEN)",
        )
    config = TelegramConfig(
        bot_token=token,
        webhook_url=req.webhook_url or os.environ.get("TELEGRAM_WEBHOOK_URL"),
        webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
        allowed_usernames=req.allowed_usernames
        or [
            u.strip().lstrip("@")
            for u in os.environ.get("TELEGRAM_ALLOWED_USERNAMES", "").split(",")
            if u.strip()
        ],
        default_workspace=req.default_workspace,
    )
    service = TelegramBotService(config, conversation_service)
    _set_telegram_service(service)
    await service.start()
    return {"status": "started", **service.get_status()}


@telegram_router.post("/stop")
async def telegram_stop(
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
) -> dict:
    """Stop the Telegram bot."""
    await service.stop()
    _set_telegram_service(None)
    return {"status": "stopped"}


@telegram_router.get("/chats")
async def telegram_chats(
    service: TelegramBotService = Depends(_get_or_create_telegram_service),
) -> list:
    """List active Telegram chat sessions."""
    return [
        {
            "chat_id": s.chat_id,
            "chat_title": s.chat_title,
            "chat_username": s.chat_username,
            "conversation_id": str(s.conversation_id) if s.conversation_id else None,
            "status": s.status,
            "message_count": s.message_count,
            "last_activity": s.last_activity.isoformat(),
        }
        for s in service._chat_sessions.values()
    ]
