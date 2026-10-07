"""Telegram bot service for the OpenHands Agent Server.

Manages the Telegram bot lifecycle, maps Telegram chats to OpenHands
conversations, and streams agent responses back to Telegram users.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.models import StartConversationRequest
from openhands.agent_server.telegram_config import (
    TelegramChatSession,
    TelegramConfig,
)
from openhands.agent_server.telegram_subscriber import _TelegramEventSubscriber
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

__all__ = [
    "TelegramBotService",
    "TelegramChatSession",
    "TelegramConfig",
]


class TelegramBotService:
    """Manages the Telegram bot and its integration with OpenHands."""

    def __init__(
        self,
        config: TelegramConfig,
        conversation_service: ConversationService,
        secrets_store=None,
    ) -> None:
        self.config = config
        self._conversation_service = conversation_service
        self._secrets_store = secrets_store
        self._chat_sessions: dict[int, TelegramChatSession] = {}
        self._subscribers: dict[int, UUID] = {}
        self._lock = asyncio.Lock()
        # Recently processed Telegram update ids. Telegram redelivers
        # updates on polling races; without this, one /new or message
        # executes (and replies) multiple times.
        self._seen_updates: deque[int] = deque(maxlen=1000)
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
            for chat_id in list(self._subscribers):
                session = self._chat_sessions.get(chat_id)
                if session and session.conversation_id:
                    await self._unsubscribe_chat(chat_id, session.conversation_id)

        assert self._app is not None
        # PTB raises RuntimeError when stopping an app that never reached
        # running state (e.g. polling failed during start). Stop best-effort
        # so /stop always converges instead of 500ing.
        try:
            if self.config.webhook_url:
                with suppress(Exception):
                    await self._app.bot.delete_webhook()
            if self._app.updater is not None:
                with suppress(RuntimeError):
                    await self._app.updater.stop()
            if self._app.running:
                await self._app.stop()
        except RuntimeError as exc:
            logger.warning(f"Telegram stop raced PTB state: {exc}")
        finally:
            with suppress(Exception):
                await self._app.shutdown()
            self._started = False
        logger.info("Telegram bot stopped")

    def get_chat_sessions(self) -> list[TelegramChatSession]:
        """Snapshot of active chat sessions (router serializes these)."""
        return list(self._chat_sessions.values())

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
        if self._is_duplicate_update(update):
            return
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
            "/new [profile] — Start a new conversation\n"
            "/status — Show bot status\n"
            "/stop — Stop current conversation\n"
            "/help — Show this help"
        )

    async def _cmd_help(self, update, _context) -> None:
        await self._cmd_start(update, _context)

    async def _cmd_status(self, update, _context) -> None:
        if self._is_duplicate_update(update):
            return
        status = self.get_status()
        await update.message.reply_text(
            f"📊 Status: {status['status']}\n"
            f"Active chats: {status['active_chats']}\n"
            f"Total messages: {status['total_messages']}"
        )

    def _resolve_profile(self, profile_name: str | None):
        """Profile by name (or the configured default) from the store.

        Falls back to the only available profile when the requested name
        doesn't exist, so a missing `default` never blocks chatting.
        """
        from openhands.agent_server.persistence import get_agent_profile_store

        store = get_agent_profile_store()
        name = (profile_name or self.config.agent_profile_name or "default").strip()
        try:
            return store.load(name)
        except FileNotFoundError:
            available = [p.removesuffix(".json") for p in store.list()]
            if len(available) == 1:
                return store.load(available[0])
            raise

    def _conversation_secrets(self, profile) -> dict[str, Any]:
        """User secrets the profile is allowed to receive.

        Mirrors the server's own scoping: ``secret_refs=None`` exposes all
        stored secrets, otherwise only the listed names.
        """
        from pydantic import SecretStr

        from openhands.agent_server.persistence import get_secrets_store
        from openhands.sdk.secret.secrets import StaticSecret

        try:
            store = self._secrets_store or get_secrets_store()
            secrets = store.load()
            stored_names = list(secrets.custom_secrets) if secrets else []
        except Exception as exc:
            logger.warning(f"Could not read secrets store for Telegram: {exc}")
            return {}
        refs = getattr(profile, "secret_refs", None)
        names = stored_names if refs is None else [n for n in refs if n in stored_names]
        result: dict[str, Any] = {}
        for secret_name in names:
            try:
                value = store.get_secret(secret_name)
            except Exception as exc:
                logger.warning(f"Could not read secret for Telegram: {exc}")
                continue
            if value:
                result[secret_name] = StaticSecret(value=SecretStr(value))
        return result

    async def _cmd_new(self, update, _context) -> None:
        if self._is_duplicate_update(update):
            return
        chat_id = update.effective_chat.id
        parts = (update.message.text or "").split()
        profile_name = parts[1] if len(parts) > 1 else None
        try:
            profile = await asyncio.to_thread(self._resolve_profile, profile_name)
        except FileNotFoundError as exc:
            await update.message.reply_text(f"⛔ {exc}")
            return
        async with self._lock:
            if chat_id in self._chat_sessions:
                old = self._chat_sessions[chat_id]
                if old.conversation_id and chat_id in self._subscribers:
                    await self._unsubscribe_chat(chat_id, old.conversation_id)
            self._chat_sessions[chat_id] = TelegramChatSession(
                chat_id=chat_id,
                chat_title=update.effective_chat.title,
                chat_username=update.effective_user.username,
                profile_name=profile.name,
            )
        used = profile.name
        await update.message.reply_text(
            f"🆕 New conversation started (profile: {used}). Send me a message!"
        )

    async def _cmd_stop(self, update, _context) -> None:
        if self._is_duplicate_update(update):
            return
        chat_id = update.effective_chat.id
        async with self._lock:
            session = self._chat_sessions.get(chat_id)
            if session:
                session.status = "idle"
                if session.conversation_id and chat_id in self._subscribers:
                    await self._unsubscribe_chat(chat_id, session.conversation_id)
        await update.message.reply_text("🛑 Conversation stopped.")

    async def _on_message(self, update, _context) -> None:
        user = update.effective_user
        if not self._is_allowed(user):
            return
        if self._is_duplicate_update(update):
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
            detail = str(exc)[:500]
            if "Authentication required" in detail:
                detail += (
                    "\n\nAdd your provider key in Canvas → Settings → "
                    "Secrets, then /new."
                )
            await update.message.reply_text(f"❌ Error: {detail}")
            session.status = "error"

    def _is_duplicate_update(self, update) -> bool:
        """True when this Telegram update was already processed.

        The check-and-record is synchronous (no awaits), so concurrent
        handler tasks can't both slip through for the same update.
        """
        update_id = getattr(update, "update_id", None)
        if update_id is None:
            return False
        if update_id in self._seen_updates:
            return True
        self._seen_updates.append(update_id)
        return False

    async def _handle_chat_message(
        self, chat_id: int, text: str, session: TelegramChatSession
    ) -> None:
        # Guard the lazy first-message creation: duplicate deliveries of
        # the same message race here while conversation_id is still None.
        async with self._lock:
            if session.conversation_id is None:
                conv = await self._create_conversation(chat_id, session.profile_name)
                if conv is None:
                    await self._send_message(
                        chat_id, "❌ Failed to create conversation"
                    )
                    return
                session.conversation_id = conv["id"]

        conv_id = session.conversation_id
        assert conv_id is not None
        from openhands.sdk import Message, TextContent

        event_service = await self._conversation_service.get_event_service(conv_id)
        if event_service is None:
            await self._send_message(chat_id, "❌ Conversation unavailable")
            return
        if chat_id not in self._subscribers:
            subscriber = _TelegramEventSubscriber(
                chat_id=chat_id,
                send_message=self._send_message,
            )
            self._subscribers[chat_id] = await event_service.subscribe_to_events(
                subscriber
            )

        # Normal turn on the shared conversation (visible in the UI),
        # not ask_agent: that forks a side session most agents can't fork.
        await event_service.send_message(
            Message(role="user", content=[TextContent(text=text)]), run=True
        )
        session.status = "idle"

    async def _unsubscribe_chat(self, chat_id: int, conv_id: UUID | None) -> None:
        sub_id = self._subscribers.pop(chat_id, None)
        if sub_id is None or conv_id is None:
            return
        try:
            event_service = await self._conversation_service.get_event_service(conv_id)
            if event_service is not None:
                await event_service.unsubscribe_from_events(sub_id)
        except Exception as exc:
            logger.warning(f"Failed to unsubscribe Telegram chat {chat_id}: {exc}")

    async def _create_conversation(
        self, chat_id: int, profile_name: str | None = None
    ) -> dict[str, Any] | None:
        from openhands.sdk.workspace import LocalWorkspace

        try:
            profile = await asyncio.to_thread(self._resolve_profile, profile_name)
            req = StartConversationRequest(
                workspace=LocalWorkspace(working_dir=self.config.default_workspace),
                tags={"source": "telegram", "chatid": str(chat_id)},
                agent_profile_id=profile.id,
                secrets=self._conversation_secrets(profile),
            )
            info, _ = await self._conversation_service.start_conversation(req)
            return {"id": info.id}
        except FileNotFoundError as exc:
            logger.error(f"No agent profile for Telegram chat {chat_id}: {exc}")
            await self._send_message(
                chat_id,
                f"⛔ {exc}\nCreate one in Settings → Agents, or /new <profile>.",
            )
            return None
        except Exception as exc:
            logger.error(f"Failed to create conversation for {chat_id}: {exc}")
            return None

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
