"""Conversation-event subscriber that forwards agent output to Telegram."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from openhands.agent_server.pub_sub import Subscriber
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

    @property
    def _buffered_text(self) -> str:
        return "".join(self._buffer)

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
            # value arrives serialized: plain string or str-enum member.
            if event.key == "execution_status" and isinstance(event.value, str):
                status_value = event.value.lower()
                if status_value == "error":
                    text = "❌ Conversation error"
                if status_value in ("idle", "error"):
                    await self._flush()

        if not text:
            return
        # Streaming deltas accumulate silently; each complete agent
        # MessageEvent flushes immediately so answers go out the moment
        # they're done instead of waiting on run-end state propagation.
        # The final event usually repeats the streamed full text, so
        # collapse overlaps instead of duplicating. Flushing only whole
        # messages keeps markdown (e.g. code fences) intact.
        is_final = isinstance(event, MessageEvent)
        buffered = self._buffered_text
        if buffered and (buffered in text or text in buffered):
            self._buffer = [text if len(text) > len(buffered) else buffered]
        elif text != buffered:
            self._buffer.append(text)
        if is_final or len(self._buffered_text) > 3500:
            await self._flush()

    async def flush(self) -> None:
        """Send the aggregated turn as one message, if any."""
        await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        msg = self._buffered_text
        self._buffer.clear()
        if len(msg) > 4000:
            msg = msg[:3990] + "\n... (truncated)"
        try:
            await self._send(self.chat_id, msg)
        except Exception as exc:
            logger.warning(f"Failed to send Telegram message: {exc}")

    async def close(self) -> None:
        await self._flush()
