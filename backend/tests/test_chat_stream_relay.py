"""Phase 0 go/no-go tests for docs/71 — ChatStreamRelay dual-output equivalence.

These tests assert that ``stream_to_http`` and ``run_to_session`` produce
identical session-side effects (upserted messages) for the same input stream.
U1 passing is the hard gate for proceeding to Phase 1.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.models.chat import (
    ChatRequest,
    ChatSession,
    ChatSessionMessage,
    ChatSessionMessageTimelineItem,
    ChatTokenUsage,
)
from app.services.chat_stream_relay import ChatStreamRelay, StreamInterrupted

# ── Deterministic fixtures ──────────────────────────────────────────

FIXED_NOW = 1_700_000_000_000

EVENT_FIXTURE_PAYLOADS = [
    {"type": "thinking_start", "assistant_turn_index": 0, "content_index": 0},
    {"type": "thinking_delta", "delta": "Let me analyze this.",
     "assistant_turn_index": 0, "content_index": 0},
    {"type": "thinking_end", "content": "Let me analyze this.",
     "assistant_turn_index": 0, "content_index": 0},
    {"type": "text_delta", "delta": "Based on my analysis,",
     "assistant_turn_index": 0, "content_index": 1},
    {"type": "text_delta", "delta": " the answer is 42.",
     "assistant_turn_index": 0, "content_index": 1},
    {"type": "tool_start", "tool_call_id": "call_abc",
     "tool_name": "read_file", "label": "Reading config.json"},
    {"type": "tool_end", "tool_call_id": "call_abc",
     "tool_name": "read_file", "is_error": False},
    {"type": "tool_report", "tool_call_id": "call_abc",
     "tool_name": "read_file", "summary": "File read: config.json (42 lines)"},
    {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 50,
     "total_tokens": 150}},
    {"type": "response", "response": {
     "message": "Based on my analysis, the answer is 42.",
     "thinking": "Let me analyze this.",
     "metadata": {"token_usage": {"input_tokens": 100, "output_tokens": 50,
       "total_tokens": 150}}}},
    {"type": "done"},
]

ERROR_FIXTURE = [
    {"type": "thinking_start", "assistant_turn_index": 0, "content_index": 0},
    {"type": "text_delta", "delta": "Partial response",
     "assistant_turn_index": 0, "content_index": 1},
    {"type": "error", "detail": "Upstream timeout"},
]


def payloads_to_sse_bytes(payloads: list[dict]) -> list[bytes]:
    """Return SSE-encoded bytes as manager_service.stream_chat yields them."""
    return [f'data: {json.dumps(p)}\n\n'.encode() for p in payloads]


# ── RecordingChatSessionService mock ────────────────────────────────

class RecordingChatSessionService:
    """Records upsert_message / publish_stream_event calls and stores messages
    by (project_id, session_id) so that ``get_session`` can read them back.
    """

    def __init__(self) -> None:
        self.upsert_calls: list[tuple[str, str, ChatSessionMessage]] = []
        self.published_events: list[dict] = []
        self._messages: dict[tuple[str, str], list[ChatSessionMessage]] = {}

    def upsert_message(self, project_id: str, session_id: str, message: ChatSessionMessage) -> None:
        key = (project_id, session_id)
        msgs = self._messages.setdefault(key, [])
        for i, m in enumerate(msgs):
            if m.id == message.id:
                msgs[i] = message
                break
        else:
            msgs.append(message)
        self.upsert_calls.append((project_id, session_id, message.model_copy(deep=True)))

    def publish_stream_event(
        self,
        project_id: str,
        session_id: str,
        *,
        message_id: str,
        event: dict,
        seq: int | None = None,
        revision: int | None = None,
    ) -> None:
        self.published_events.append({
            "project_id": project_id,
            "session_id": session_id,
            "message_id": message_id,
            "event": event,
            "seq": seq,
        })

    def get_session(self, project_id: str, session_id: str) -> ChatSession:
        key = (project_id, session_id)
        msgs = self._messages.get(key, [])
        return ChatSession(
            session_id=session_id,
            summary="",
            messages=list(msgs),
            created_at=str(FIXED_NOW),
            updated_at=str(FIXED_NOW),
            revision=len(msgs),
        )


# ── Test classes ────────────────────────────────────────────────────

class ChatStreamRelayDualOutputTest(unittest.TestCase):
    """U1 — Assert ``stream_to_http`` and ``run_to_session`` produce identical
    final session messages for the same fixture stream.
    """

    def setUp(self) -> None:
        # Patch _now_ms so all timeline timestamps are deterministic.
        self._now_patch = patch.object(ChatStreamRelay, "_now_ms", return_value=FIXED_NOW)
        self._now_patch.start()
        # Patch monotonic so all events fall inside one persist window.
        self._mono_patch = patch("time.monotonic", return_value=0.0)
        self._mono_patch.start()

    def tearDown(self) -> None:
        self._mono_patch.stop()
        self._now_patch.stop()

    def _make_relay(self, chat_svc: RecordingChatSessionService) -> ChatStreamRelay:
        mgr = MagicMock(spec=ChatStreamRelay.__init__.__code__.co_varnames)  # type: ignore[arg-type]
        return ChatStreamRelay(chat_svc, mgr)  # type: ignore[arg-type]

    def _mock_manager_service(self, payloads: list[dict]) -> MagicMock:
        """Return a ManagerService mock whose ``stream_chat`` yields SSE bytes."""
        mgr = MagicMock()
        mgr.stream_chat.return_value = iter(payloads_to_sse_bytes(payloads))
        return mgr

    def test_stream_to_http_matches_run_to_session(self) -> None:
        """U1: both paths produce field-for-field identical final messages."""
        chat_run = RecordingChatSessionService()
        chat_http = RecordingChatSessionService()

        mgr_run = self._mock_manager_service(EVENT_FIXTURE_PAYLOADS)
        mgr_http = self._mock_manager_service(EVENT_FIXTURE_PAYLOADS)

        relay_run = ChatStreamRelay(chat_run, mgr_run)
        relay_http = ChatStreamRelay(chat_http, mgr_http)

        request = ChatRequest(message="hi")

        # --- Path A: run_to_session (synchronous, raises on error) ---
        relay_run.run_to_session("proj", "sess", request, message_id="mgr_test")

        # --- Path B: stream_to_http (generator, yields SSE bytes) ---
        http_bytes = list(
            relay_http.stream_to_http("proj", "sess", request, message_id="mgr_test")
        )

        # --- Extract final upserted messages ---
        self.assertTrue(chat_run.upsert_calls, "run_to_session should have upserted")
        self.assertTrue(chat_http.upsert_calls, "stream_to_http should have upserted")

        msg_run = chat_run.upsert_calls[-1][2]
        msg_http = chat_http.upsert_calls[-1][2]

        # --- Field-for-field assertions ---
        self.assertEqual(msg_run.id, "mgr_test")
        self.assertEqual(msg_http.id, "mgr_test")
        self.assertEqual(msg_run.state, "done")
        self.assertEqual(msg_http.state, "done")
        self.assertEqual(
            msg_run.content,
            "Based on my analysis, the answer is 42.",
        )
        self.assertEqual(msg_run.content, msg_http.content)
        self.assertEqual(msg_run.thinking, "Let me analyze this.")
        self.assertEqual(msg_run.thinking, msg_http.thinking)

        # Timeline length
        self.assertEqual(len(msg_run.timeline or []), len(msg_http.timeline or []))
        self.assertTrue(msg_run.timeline)
        self.assertTrue(msg_http.timeline)

        for item_run, item_http in zip(msg_run.timeline or [], msg_http.timeline or []):
            self.assertEqual(item_run.id, item_http.id)
            self.assertEqual(item_run.kind, item_http.kind)
            self.assertEqual(item_run.status, item_http.status)
            self.assertEqual(item_run.content, item_http.content)
            self.assertEqual(item_run.tool_name, item_http.tool_name)

        # Token usage
        self.assertIsNotNone(msg_run.token_usage)
        self.assertIsNotNone(msg_http.token_usage)
        self.assertEqual(
            msg_run.token_usage.total_tokens if msg_run.token_usage else 0,
            msg_http.token_usage.total_tokens if msg_http.token_usage else 0,
        )
        self.assertEqual(
            (msg_run.token_usage.total_tokens if msg_run.token_usage else 0),
            150,
        )

        # stream_to_http must yield at least one non-empty bytes chunk
        self.assertTrue(http_bytes, "stream_to_http should yield at least one chunk")
        self.assertTrue(any(len(b) > 0 for b in http_bytes), "yielded chunks must be non-empty")

    def test_stream_to_http_error_path_matches_run_to_session(self) -> None:
        """U1b: error fixture — run_to_session raises, stream_to_http yields
        error SSE bytes and settles message to error without raising during
        consumption.
        """
        chat_run = RecordingChatSessionService()
        chat_http = RecordingChatSessionService()

        mgr_run = self._mock_manager_service(ERROR_FIXTURE)
        mgr_http = self._mock_manager_service(ERROR_FIXTURE)

        relay_run = ChatStreamRelay(chat_run, mgr_run)
        relay_http = ChatStreamRelay(chat_http, mgr_http)

        request = ChatRequest(message="hi")

        # --- Path A: run_to_session must raise RuntimeError ---
        with self.assertRaises(RuntimeError):
            relay_run.run_to_session("proj", "sess", request, message_id="mgr_test")

        # --- Path B: stream_to_http must be consumable without raising ---
        http_bytes: list[bytes] = []
        try:
            for chunk in relay_http.stream_to_http("proj", "sess", request, message_id="mgr_test"):
                http_bytes.append(chunk)
        except RuntimeError:
            self.fail("stream_to_http should not raise during normal consumption")

        # --- Both paths should have settled the message to "error" ---
        self.assertTrue(chat_run.upsert_calls, "run_to_session should have upserted")
        self.assertTrue(chat_http.upsert_calls, "stream_to_http should have upserted")

        msg_run = chat_run.upsert_calls[-1][2]
        msg_http = chat_http.upsert_calls[-1][2]

        self.assertEqual(msg_run.state, "error")
        self.assertEqual(msg_http.state, "error")
        self.assertEqual(msg_run.content, msg_http.content)
        self.assertEqual(msg_run.content, "Partial response")

        # At least one yielded SSE bytes contains b'error'
        error_chunks = [b for b in http_bytes if b"error" in b]
        self.assertTrue(error_chunks, "at least one yielded chunk must contain b'error'")


class ChatStreamRelayInterruptTest(unittest.TestCase):
    """U7 — Assert closing ``stream_to_http`` settles running tools to ``interrupted``."""

    def test_close_settles_running_tool_to_interrupted(self) -> None:
        """Closing the HTTP generator after a ``tool_start`` must persist the
        manager message with the running tool timeline item marked
        ``interrupted`` and message state ``done``.
        """
        chat_svc = RecordingChatSessionService()
        mgr = MagicMock()

        def mock_stream():
            yield f'data: {json.dumps({"type": "tool_start", "tool_call_id": "call_slow", "tool_name": "read_file", "label": "Reading large file"})}\n\n'.encode()
            # This chunk is never consumed; gen.close() interrupts the stream.
            yield f'data: {json.dumps({"type": "text_delta", "delta": "partial ", "assistant_turn_index": 0, "content_index": 1})}\n\n'.encode()

        mgr.stream_chat.return_value = mock_stream()
        relay = ChatStreamRelay(chat_svc, mgr)
        request = ChatRequest(message="read a big file")

        gen = relay.stream_to_http("proj", "sess", request, message_id="mgr_interrupt")
        chunk = next(gen)
        self.assertIn(b"tool_start", chunk)
        gen.close()

        # The relay should have upserted the final interrupted snapshot.
        self.assertTrue(chat_svc.upsert_calls, "stream_to_http should have upserted")
        final_message = chat_svc.upsert_calls[-1][2]
        self.assertEqual(final_message.state, "done",
                         f"Manager message state should be 'done', got {final_message.state}")

        tool_items = [item for item in (final_message.timeline or []) if item.kind == "tool"]
        self.assertTrue(tool_items, "Expected at least one tool timeline item")
        self.assertEqual(tool_items[0].status, "interrupted",
                         f"Tool item status should be 'interrupted', got {tool_items[0].status}")
