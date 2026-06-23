"""Phase 1 endpoint tests for docs/71 — events SSE revision and seq tests.

U5: Assert that message_upsert events carry integer revision and stream_event
events carry integer seq, with seq monotonically increasing.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.deps import (
    get_chat_session_service,
    get_chat_stream_relay,
    get_manager_service,
    get_project_service,
)
from app.core.config import Settings, get_settings
from app.main import app
from app.services.chat_session_service import ChatSessionService
from app.services.project_service import ProjectService

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


def payloads_to_sse_bytes(payloads: list[dict]) -> list[bytes]:
    return [f'data: {json.dumps(p)}\n\n'.encode() for p in payloads]


class ChatEventsSSETest(unittest.TestCase):
    """U5 — events SSE carries revision and seq."""

    def setUp(self) -> None:
        self._original_data_root = get_settings().data_root
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root, chat_single_source=True)
        get_settings.cache_clear()

        # Patch get_settings at all import sites so the app sees our temp settings
        self._settings_patch = patch("app.core.config.get_settings", return_value=self.settings)
        self._settings_patch.start()
        self._chat_settings_patch = patch("app.api.chat.get_settings", return_value=self.settings)
        self._chat_settings_patch.start()

        # Use unique project id per test to avoid xdist collisions
        self.project_id = f"test-proj-{uuid4().hex[:8]}"
        self.project_service = ProjectService()
        self.project_service.create_project(self.project_id, "Test", "test goal")

        self.client = TestClient(app)

        app.dependency_overrides[get_project_service] = lambda: self.project_service

        from app.services.chat_stream_relay import ChatStreamRelay

        self.chat_session_service = ChatSessionService(self.project_service, manager_auto_service=None)
        self.manager_service_mock = MagicMock()
        self.chat_stream_relay = ChatStreamRelay(self.chat_session_service, self.manager_service_mock)

        app.dependency_overrides[get_chat_session_service] = lambda: self.chat_session_service
        app.dependency_overrides[get_chat_stream_relay] = lambda: self.chat_stream_relay
        app.dependency_overrides[get_manager_service] = lambda: self.manager_service_mock

    def tearDown(self) -> None:
        self._chat_settings_patch.stop()
        self._settings_patch.stop()
        get_settings.cache_clear()
        get_project_service.cache_clear()
        get_chat_session_service.cache_clear()
        get_chat_stream_relay.cache_clear()
        get_manager_service.cache_clear()
        app.dependency_overrides.clear()
        shutil.rmtree(self.data_root, ignore_errors=True)

    def _create_session(self, project_id: str | None = None) -> str:
        pid = project_id or self.project_id
        resp = self.client.post(f"/api/projects/{pid}/chat-sessions", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["session"]["session_id"]

    def test_events_sse_carries_revision_and_seq(self) -> None:
        """U5: message_upsert events carry revision, stream_event events carry seq."""
        session_id = self._create_session()
        self.manager_service_mock.stream_chat.return_value = iter(payloads_to_sse_bytes(EVENT_FIXTURE_PAYLOADS))

        # Collect events from the SSE subscription using the same chat_session_service
        # that the relay will publish to. We subscribe BEFORE triggering the relay.
        event_iterator = self.chat_session_service.subscribe_events(
            self.project_id, session_id, timeout_seconds=0.5
        )

        # Trigger the relay via POST /chat-stream
        resp = self.client.post(
            f"/api/projects/{self.project_id}/chat-stream",
            json={"message": "hi", "session_id": session_id},
        )
        self.assertEqual(resp.status_code, 200)
        _ = resp.content  # consume

        # Collect events from the iterator with a timeout
        collected_events: list[dict] = []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                event = next(event_iterator)
                collected_events.append(event)
                # Stop collecting after we see a heartbeat (means queue is empty)
                if event.get("type") == "heartbeat":
                    break
            except StopIteration:
                break

        # Filter to relevant event types
        message_upserts = [e for e in collected_events if e.get("type") == "message_upsert"]
        stream_events = [e for e in collected_events if e.get("type") == "stream_event"]

        # Assert message_upsert events carry integer revision
        self.assertTrue(message_upserts, "Should have received at least one message_upsert event")
        for evt in message_upserts:
            revision = evt.get("revision")
            self.assertIsInstance(revision, int, f"revision should be int, got {type(revision)}")
            self.assertGreater(revision, 0, "revision should be > 0")

        # Assert stream_event events carry integer seq
        self.assertTrue(stream_events, "Should have received at least one stream_event event")
        seqs = []
        for evt in stream_events:
            seq = evt.get("seq")
            self.assertIsInstance(seq, int, f"seq should be int, got {type(seq)}")
            seqs.append(seq)

        # Assert seq monotonically increases
        self.assertEqual(seqs, sorted(seqs), f"seq should be monotonically increasing: {seqs}")
        self.assertEqual(len(seqs), len(set(seqs)), f"seq values should be unique: {seqs}")

        # Last message_upsert revision should be > 1
        last_revision = message_upserts[-1].get("revision")
        self.assertGreater(last_revision, 1, f"last revision should be > 1, got {last_revision}")

        # seq should start from 1
        self.assertEqual(min(seqs), 1, f"seq should start at 1, got min={min(seqs)}")


if __name__ == "__main__":
    unittest.main()
