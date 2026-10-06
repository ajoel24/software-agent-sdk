"""Tests for Telegram per-chat agent profiles (/new [profile])."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from openhands.sdk.profiles.agent_profile import OpenHandsAgentProfile
from openhands.sdk.profiles.agent_profile_store import AgentProfileStore


def _stub_telegram_lib():
    telegram = ModuleType("telegram")
    telegram.Update = MagicMock
    ext = ModuleType("telegram.ext")
    ext.Application = MagicMock()
    ext.CommandHandler = MagicMock()
    ext.MessageHandler = MagicMock()
    ext.filters = MagicMock()
    sys.modules["telegram"] = telegram
    sys.modules["telegram.ext"] = ext


_stub_telegram_lib()

from openhands.agent_server import telegram_service as tg  # noqa: E402


@pytest.fixture
def profile_store(tmp_path, monkeypatch):
    store = AgentProfileStore(base_dir=tmp_path / "profiles")
    store.save(OpenHandsAgentProfile(name="default", llm_profile_ref="x"))
    store.save(OpenHandsAgentProfile(name="coder", llm_profile_ref="x"))
    monkeypatch.setattr(
        "openhands.agent_server.persistence.get_agent_profile_store",
        lambda: store,
    )
    return store


@pytest.fixture
def service(profile_store):
    tg._set_telegram_service(None)
    svc = tg.TelegramBotService(
        tg.TelegramConfig(bot_token="t"),
        MagicMock(),
    )
    yield svc
    tg._set_telegram_service(None)


def _update(text="/new", chat_id=1):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.title = None
    update.effective_user.username = "ajoel24"
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_new_defaults_to_default_profile(service, profile_store):
    update = _update("/new")
    await service._cmd_new(update, MagicMock())

    session = service._chat_sessions[1]
    assert session.profile_name is None
    assert session.conversation_id is None
    reply = update.message.reply_text.await_args.args[0]
    assert "profile: default" in reply


@pytest.mark.asyncio
async def test_new_with_named_profile(service):
    update = _update("/new coder")
    await service._cmd_new(update, MagicMock())

    assert service._chat_sessions[1].profile_name == "coder"
    reply = update.message.reply_text.await_args.args[0]
    assert "profile: coder" in reply


@pytest.mark.asyncio
async def test_new_unknown_profile_rejected(service):
    update = _update("/new nope")
    await service._cmd_new(update, MagicMock())

    assert 1 not in service._chat_sessions
    reply = update.message.reply_text.await_args.args[0]
    assert reply.startswith("⛔")


@pytest.mark.asyncio
async def test_create_conversation_passes_profile_id(service, profile_store):
    info = MagicMock()
    info.id = UUID(int=7)
    service._conversation_service.start_conversation = AsyncMock(
        return_value=(info, True)
    )

    result = await service._create_conversation(1, "coder")

    assert result == {"id": info.id}
    req = service._conversation_service.start_conversation.await_args.args[0]
    assert req.agent_profile_id == profile_store.load("coder").id


@pytest.mark.asyncio
async def test_resolve_uses_configured_default(service, profile_store):
    service.config.agent_profile_name = "coder"
    assert service._resolve_profile_id(None) == profile_store.load("coder").id
