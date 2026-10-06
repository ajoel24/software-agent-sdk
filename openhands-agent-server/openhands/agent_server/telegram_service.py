"""Telegram bot service for the OpenHands Agent Server.

Manages the Telegram bot lifecycle, maps Telegram chats to OpenHands
conversations, and streams agent responses back to Telegram users.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.models import StartConversationRequest
from openhands.agent_server.pub_sub import PubSub, Subscriber
from openhands.sdk.event import Event
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)


class _TelegramEventSubscriber(Subscriber[Event]):
    """Forwards conversation events to a Telegram chat."""

    receives_streaming_deltas = True

    def __init__(
        self,
        chat_id: int,
        send_message: Callable[[int, str], Awaitable[None]],
    ) -> None:
        self.chat_id = chat_id
        self._send = send_message
        self._buffer: list[str] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._last_text = ""

    async def __call__(self, event: Event) -> None:
        from openhands.sdk.event.llm_convertible.message import MessageEvent
        from openhands.sdk.event.streaming_delta import StreamingDeltaEvent
        from openhands.sdk.llm import content_to_str

        text: str | None = None
        if isinstance(event, StreamingDeltaEvent):
            text = event.content
        elif isinstance(event, MessageEvent):
            if event.source == "agent":
                text = "\n".join(content_to_str(event.llm_message.content))
        elif isinstance(event, ConversationStateUpdateEvent):
            if event.key == "execution_status" and event.value == "ERROR":
                text = "❌ Conversation error"

        if not text or text == self._last_text:
            return
        self._last_text = text
        self._buffer.append(text)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(0.5)
        if not self._buffer:
            return
        msg = "".join(self._buffer)
        self._buffer.clear()
        if len(msg) > 4000:
            msg = msg[:3990] + "\n... (truncated)"
        try:
            await self._send(self.chat_id, msg)
        except Exception as exc:
            logger.warning(f"Failed to send Telegram message: {exc}")

    async def close(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task


@dataclass
class TelegramChatSession:
    """Tracks the link between a Telegram chat and an OpenHands conversation."""

    chat_id: int
    chat_title: str | None = None
    chat_username: str | None = None
    conversation_id: UUID | None = None
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
    max_concurrent_chats: int = 10


class TelegramBotService:
    """Manages the Telegram bot and its integration with OpenHands."""

    def __init__(
        self,
        config: TelegramConfig,
        conversation_service: ConversationService,
    ) -> None:
        self.config = config
        self._conversation_service = conversation_service
        self._chat_sessions: dict[int, TelegramChatSession] = {}
        self._subscribers: dict[int, UUID] = {}
        self._pubsubs: dict[UUID, PubSub[Event]] = {}
        self._lock = asyncio.Lock()
        self._started = False

        # Lazy import: telegram is an optional dependency.
        try:
            from telegram import Update  # noqa: F401
            from telegram.ext import Application  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "python-telegram-bot is required for Telegram integration. "
                "Install with: pip install openhands-agent-server[telegram]"
            ) from exc

        self._Update = Update
        self._app = Application.builder().token(config.bot_token).build()

        self._register_handlers()

    def _register_handlers(self) -> None:
        from telegram.ext import CommandHandler, MessageHandler, filters  # noqa: F401

        assert self._app is not None
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("new", self._cmd_new))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

    async def start(self) -> None:
        if self._started:
            return
        assert self._app is not None
        await self._app.initialize()
        await self._app.start()
        if self.config.webhook_url:
            await self._app.bot.set_webhook(
                url=self.config.webhook_url,
                secret_token=self.config.webhook_secret,
            )
            logger.info(f"Telegram webhook set: {self.config.webhook_url}")
        else:
            assert self._app.updater is not None
            await self._app.updater.start_polling()
            logger.info("Telegram bot polling started")
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        async with self._lock:
            for chat_id, sub_id in list(self._subscribers.items()):
                session = self._chat_sessions.get(chat_id)
                if session and session.conversation_id:
                    pubsub = self._pubsubs.get(session.conversation_id)
                    if pubsub:
                        pubsub.unsubscribe(sub_id)

        assert self._app is not None
        if self.config.webhook_url:
            await self._app.bot.delete_webhook()
        await self._app.stop()
        await self._app.shutdown()
        self._started = False
        logger.info("Telegram bot stopped")

    def is_running(self) -> bool:
        return self._started

    def get_status(self) -> dict[str, Any]:
        return {
            "status": "running" if self._started else "stopped",
            "active_chats": len(self._chat_sessions),
            "total_messages": sum(
                s.message_count for s in self._chat_sessions.values()
            ),
        }

    async def handle_webhook(self, payload: dict[str, Any]) -> None:
        if not self._app:
            raise RuntimeError("Bot not started")
        update = self._Update.de_json(payload, self._app.bot)
        await self._app.process_update(update)

    async def _cmd_start(self, update, _context) -> None:
        user = update.effective_user
        if not self._is_allowed(user):
            await update.message.reply_text(
                "⛔ You are not authorized to use this bot."
            )
            return
        await update.message.reply_text(
            f"👋 Hello {user.first_name}!\n\n"
            "Send me a message and I'll help you with coding tasks.\n\n"
            "Commands:\n"
            "/new — Start a new conversation\n"
            "/status — Show bot status\n"
            "/stop — Stop current conversation\n"
            "/help — Show this help"
        )

    async def _cmd_help(self, update, _context) -> None:
        await self._cmd_start(update, _context)

    async def _cmd_status(self, update, _context) -> None:
        status = self.get_status()
        await update.message.reply_text(
            f"📊 Status: {status['status']}\n"
            f"Active chats: {status['active_chats']}\n"
            f"Total messages: {status['total_messages']}"
        )

    async def _cmd_new(self, update, _context) -> None:
        chat_id = update.effective_chat.id
        async with self._lock:
            if chat_id in self._chat_sessions:
                old = self._chat_sessions[chat_id]
                if old.conversation_id and chat_id in self._subscribers:
                    pubsub = self._pubsubs.get(old.conversation_id)
                    if pubsub:
                        pubsub.unsubscribe(self._subscribers[chat_id])
                    del self._subscribers[chat_id]
            self._chat_sessions[chat_id] = TelegramChatSession(
                chat_id=chat_id,
                chat_title=update.effective_chat.title,
                chat_username=update.effective_user.username,
            )
        await update.message.reply_text(
            "🆕 New conversation started. Send me a message!"
        )

    async def _cmd_stop(self, update, _context) -> None:
        chat_id = update.effective_chat.id
        async with self._lock:
            session = self._chat_sessions.get(chat_id)
            if session:
                session.status = "idle"
                if session.conversation_id and chat_id in self._subscribers:
                    pubsub = self._pubsubs.get(session.conversation_id)
                    if pubsub:
                        pubsub.unsubscribe(self._subscribers[chat_id])
                    del self._subscribers[chat_id]
        await update.message.reply_text("🛑 Conversation stopped.")

    async def _on_message(self, update, _context) -> None:
        user = update.effective_user
        if not self._is_allowed(user):
            return
        chat_id = update.effective_chat.id
        text = update.message.text
        async with self._lock:
            if chat_id not in self._chat_sessions:
                self._chat_sessions[chat_id] = TelegramChatSession(
                    chat_id=chat_id,
                    chat_title=update.effective_chat.title,
                    chat_username=user.username,
                )
            session = self._chat_sessions[chat_id]
            session.message_count += 1
            session.last_activity = datetime.now(UTC)
            session.status = "running"
        await update.message.chat.send_action(action="typing")
        try:
            await self._handle_chat_message(chat_id, text, session)
        except Exception as exc:
            logger.error(f"Error handling Telegram message: {exc}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(exc)[:500]}")
            session.status = "error"

    async def _handle_chat_message(
        self, chat_id: int, text: str, session: TelegramChatSession
    ) -> None:
        if session.conversation_id is None:
            conv = await self._create_conversation(chat_id)
            if conv is None:
                await self._send_message(chat_id, "❌ Failed to create conversation")
                return
            session.conversation_id = conv["id"]

        conv_id = session.conversation_id
        assert conv_id is not None
        if chat_id not in self._subscribers:
            pubsub = await self._get_pubsub(conv_id)
            subscriber = _TelegramEventSubscriber(
                chat_id=chat_id,
                send_message=self._send_message,
            )
            sub_id = pubsub.subscribe(subscriber)
            self._subscribers[chat_id] = sub_id

        await self._conversation_service.ask_agent(
            conversation_id=conv_id,
            question=text,
        )
        session.status = "idle"

    async def _create_conversation(self, chat_id: int) -> dict[str, Any] | None:
        from openhands.sdk.workspace import LocalWorkspace

        try:
            req = StartConversationRequest(
                workspace=LocalWorkspace(working_dir=self.config.default_workspace),
                tags={"source": "telegram", "chatid": str(chat_id)},
            )
            info, _ = await self._conversation_service.start_conversation(req)
            return {"id": info.id}
        except Exception as exc:
            logger.error(f"Failed to create conversation for {chat_id}: {exc}")
            return None

    async def _get_pubsub(self, conversation_id: UUID) -> PubSub[Event]:
        if conversation_id not in self._pubsubs:
            # EventService owns the canonical PubSub; this is a fallback
            # for external subscribers that attach before the service
            # initializes its own.
            self._pubsubs[conversation_id] = PubSub[Event](max_subscribers=50)
        return self._pubsubs[conversation_id]

    async def _send_message(self, chat_id: int, text: str) -> None:
        if not self._app or not self._app.bot:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text[:4096],
            )
        except Exception as exc:
            logger.warning(f"Failed to send Telegram message: {exc}")

    def _is_allowed(self, user) -> bool:
        if not self.config.allowed_usernames:
            return True
        if user is None:
            return False
        return user.username in self.config.allowed_usernames


def _telegram_config_path() -> Path:
    """Location of the persisted bot config (secret file, mode 600)."""
    override = os.environ.get("TELEGRAM_CONFIG_FILE")
    if override:
        return Path(override)
    return Path.home() / ".openhands" / "telegram.json"


def save_telegram_config(config: TelegramConfig) -> None:
    """Persist the running bot config so restarts don't need re-entry."""
    path = _telegram_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot_token": config.bot_token,
        "webhook_url": config.webhook_url,
        "webhook_secret": config.webhook_secret,
        "allowed_usernames": config.allowed_usernames,
        "default_workspace": config.default_workspace,
    }
    path.write_text(json.dumps(payload, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.warning(f"Could not set permissions on {path}")


def load_telegram_config() -> TelegramConfig | None:
    """Load a previously persisted bot config, if any."""
    path = _telegram_config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = str(data.get("bot_token") or "").strip()
    if not token:
        return None
    return TelegramConfig(
        bot_token=token,
        webhook_url=data.get("webhook_url"),
        webhook_secret=data.get("webhook_secret"),
        allowed_usernames=list(data.get("allowed_usernames") or []),
        default_workspace=str(data.get("default_workspace") or "/workspace"),
    )


_telegram_service: TelegramBotService | None = None


def _get_telegram_service() -> TelegramBotService | None:
    return _telegram_service


def _set_telegram_service(service: TelegramBotService | None) -> None:
    global _telegram_service
    _telegram_service = service
