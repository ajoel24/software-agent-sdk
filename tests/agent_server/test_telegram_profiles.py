"""Tests for Telegram per-chat agent profiles (/new [profile])."""

import sys
from types import ModuleType
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from openhands.sdk.profiles.agent_profile import OpenHandsAgentProfile
from openhands.sdk.profiles.agent_profile_store import AgentProfileStore


def _stub_telegram_lib():
    sys.modules["telegram"] = cast(ModuleType, MagicMock())
    sys.modules["telegram.ext"] = cast(ModuleType, MagicMock())


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
    svc = tg.TelegramBotService(
        tg.TelegramConfig(bot_token="t"),
        MagicMock(),
    )
    yield svc


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
    assert session.profile_name == "default"
    assert session.conversation_id is None
    await_call = update.message.reply_text.await_args
    assert await_call is not None
    reply = await_call.args[0]
    assert "profile: default" in reply


@pytest.mark.asyncio
async def test_new_with_named_profile(service):
    update = _update("/new coder")
    await service._cmd_new(update, MagicMock())

    assert service._chat_sessions[1].profile_name == "coder"
    await_call = update.message.reply_text.await_args
    assert await_call is not None
    reply = await_call.args[0]
    assert "profile: coder" in reply


@pytest.mark.asyncio
async def test_configured_name_missing_uses_single_available(tmp_path, monkeypatch):
    solo_store = AgentProfileStore(base_dir=tmp_path / "solo2")
    solo_store.save(OpenHandsAgentProfile(name="solo", llm_profile_ref="x"))
    monkeypatch.setattr(
        "openhands.agent_server.persistence.get_agent_profile_store",
        lambda: solo_store,
    )
    svc = tg.TelegramBotService(
        tg.TelegramConfig(bot_token="t", agent_profile_name="ghost"),
        MagicMock(),
    )
    assert svc._resolve_profile(None).name == "solo"


@pytest.mark.asyncio
async def test_new_unknown_profile_rejected(service):
    update = _update("/new nope")
    await service._cmd_new(update, MagicMock())

    assert 1 not in service._chat_sessions
    await_call = update.message.reply_text.await_args
    assert await_call is not None
    reply = await_call.args[0]
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
    start_call = service._conversation_service.start_conversation.await_args
    assert start_call is not None
    req = start_call.args[0]
    assert req.agent_profile_id == profile_store.load("coder").id


@pytest.mark.asyncio
async def test_secrets_forwarded_when_refs_open(tmp_path):
    from openhands.agent_server.persistence.store import FileSecretsStore

    store = FileSecretsStore(persistence_dir=tmp_path / "secrets")
    store.set_secret("MY_KEY", "s3cret", "test key")
    svc = tg.TelegramBotService(
        tg.TelegramConfig(bot_token="t"), MagicMock(), secrets_store=store
    )
    profile = OpenHandsAgentProfile(name="default", llm_profile_ref="x")
    assert profile.secret_refs is None
    forwarded = svc._conversation_secrets(profile)
    assert set(forwarded) == {"MY_KEY"}
    assert forwarded["MY_KEY"].get_value() == "s3cret"


@pytest.mark.asyncio
async def test_secrets_filtered_by_refs(tmp_path):
    from openhands.agent_server.persistence.store import FileSecretsStore

    store = FileSecretsStore(persistence_dir=tmp_path / "secrets")
    store.set_secret("MY_KEY", "s3cret")
    store.set_secret("OTHER", "nope")
    svc = tg.TelegramBotService(
        tg.TelegramConfig(bot_token="t"), MagicMock(), secrets_store=store
    )
    profile = OpenHandsAgentProfile(
        name="default", llm_profile_ref="x", secret_refs=["MY_KEY"]
    )
    forwarded = svc._conversation_secrets(profile)
    assert set(forwarded) == {"MY_KEY"}


@pytest.mark.asyncio
async def test_chat_message_uses_normal_turn(service):
    event_service = MagicMock()
    event_service.subscribe_to_events = AsyncMock(return_value=UUID(int=3))
    event_service.send_message = AsyncMock()
    service._conversation_service.get_event_service = AsyncMock(
        return_value=event_service
    )
    service._chat_sessions[1] = tg.TelegramChatSession(
        chat_id=1, conversation_id=UUID(int=9)
    )

    await service._handle_chat_message(1, "hey!", service._chat_sessions[1])

    assert service._subscribers[1] == UUID(int=3)
    send_call = event_service.send_message.await_args
    assert send_call is not None
    msg = send_call.args[0]
    assert msg.role == "user" and "hey!" in str(msg.content)
    assert send_call.kwargs.get("run") is True
    # ask_agent (fork path) must not be used for chat
    service._conversation_service.ask_agent.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_update_processed_once(service):
    update = _update("/new")
    update.update_id = 42
    await service._cmd_new(update, MagicMock())
    await service._cmd_new(update, MagicMock())
    assert update.message.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_distinct_updates_each_processed(service):
    first, second = _update("/new"), _update("/new")
    first.update_id, second.update_id = 1, 2
    await service._cmd_new(first, MagicMock())
    await service._cmd_new(second, MagicMock())
    assert first.message.reply_text.await_count == 1
    assert second.message.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_streaming_turn_forwarded_whole_once():
    from openhands.sdk import Message as _Message, TextContent as _TextContent
    from openhands.sdk.event.conversation_state import (
        ConversationStateUpdateEvent,
    )
    from openhands.sdk.event.llm_convertible.message import MessageEvent
    from openhands.sdk.event.streaming_delta import StreamingDeltaEvent

    sent: list[str] = []

    async def fake_send(chat_id: int, text: str) -> None:
        sent.append(text)

    sub = tg._TelegramEventSubscriber(chat_id=1, send_message=fake_send)
    full = '```python\nprint("Hello, World!")\n```'
    for chunk in [full[:10], full[10:20], full[20:]]:
        await sub(StreamingDeltaEvent(content=chunk))
    # Nothing sent mid-stream...
    assert sent == []
    await sub(
        MessageEvent(
            source="agent",
            llm_message=_Message(role="assistant", content=[_TextContent(text=full)]),
        )
    )
    # ...and the complete agent message flushes immediately (not at run-end).
    assert sent == [full]
    await sub(ConversationStateUpdateEvent(key="execution_status", value="idle"))
    assert sent == [full]


@pytest.mark.asyncio
async def test_concurrent_first_messages_create_one_conversation(service):
    import asyncio as _asyncio

    calls = 0

    async def fake_create(chat_id, profile_name):
        nonlocal calls
        calls += 1
        await _asyncio.sleep(0.05)
        return {"id": UUID(int=calls)}

    service._create_conversation = fake_create  # type: ignore[method-assign]
    session = tg.TelegramChatSession(chat_id=1)
    event_service = MagicMock()
    event_service.subscribe_to_events = AsyncMock(return_value=UUID(int=3))
    event_service.send_message = AsyncMock()
    service._conversation_service.get_event_service = AsyncMock(
        return_value=event_service
    )

    await _asyncio.gather(
        service._handle_chat_message(1, "hi", session),
        service._handle_chat_message(1, "hi", session),
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_stop_tolerates_wedged_app(service):
    app = MagicMock()
    app.updater.stop = AsyncMock()
    app.running = False
    app.stop = AsyncMock(side_effect=RuntimeError("not running"))
    app.shutdown = AsyncMock()
    service._app = app
    service._started = True

    await service.stop()  # must not raise

    assert service._started is False
    app.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_noop_when_not_started(service):
    service._started = False
    await service.stop()
    assert service._started is False


@pytest.mark.asyncio
async def test_unsubscribe_chat(service):
    event_service = MagicMock()
    event_service.unsubscribe_from_events = AsyncMock(return_value=True)
    service._conversation_service.get_event_service = AsyncMock(
        return_value=event_service
    )
    service._subscribers[1] = UUID(int=3)

    await service._unsubscribe_chat(1, UUID(int=9))

    assert 1 not in service._subscribers
    event_service.unsubscribe_from_events.assert_awaited_once_with(UUID(int=3))


@pytest.mark.asyncio
async def test_auth_error_hint(service):
    update = _update("hello")
    update.message.chat.send_action = AsyncMock()

    async def fake_handle(chat_id, text, session):
        raise RuntimeError("Authentication required: Configure an API key")

    service._handle_chat_message = fake_handle  # type: ignore[method-assign]
    await service._on_message(update, MagicMock())
    await_call = update.message.reply_text.await_args
    assert await_call is not None
    reply = await_call.args[0]
    assert "Secrets" in reply
    assert service._chat_sessions[1].status == "error"


@pytest.mark.asyncio
async def test_resolve_uses_configured_default(service, profile_store):
    service.config.agent_profile_name = "coder"
    assert service._resolve_profile(None).id == profile_store.load("coder").id


@pytest.mark.asyncio
async def test_default_falls_back_to_single_profile(tmp_path, monkeypatch):
    from openhands.sdk.profiles.agent_profile_store import AgentProfileStore

    solo_store = AgentProfileStore(base_dir=tmp_path / "solo")
    solo_store.save(OpenHandsAgentProfile(name="solo", llm_profile_ref="x"))
    monkeypatch.setattr(
        "openhands.agent_server.persistence.get_agent_profile_store",
        lambda: solo_store,
    )
    svc = tg.TelegramBotService(tg.TelegramConfig(bot_token="t"), MagicMock())
    update = _update("/new")
    await svc._cmd_new(update, MagicMock())
    assert svc._chat_sessions[1].profile_name == "solo"
    await_call = update.message.reply_text.await_args
    assert await_call is not None
    reply = await_call.args[0]
    assert "profile: solo" in reply
