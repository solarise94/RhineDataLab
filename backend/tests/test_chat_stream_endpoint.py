"""Phase 1 endpoint tests for docs/71 — manual chat path via relay.

These tests assert that POST /chat-stream (with chat_single_source=True):
- writes the user message into the session
- persists manager messages without requiring a frontend PUT
- does not overwrite background writes (depjob_terminal, etc.)
- returns X-Blueprint-Session-Id header
- settles manager messages on client disconnect
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
    get_manager_command_service,
    get_manager_service,
    get_project_service,
)
from app.core.config import Settings, get_settings
from app.main import app
from app.models.chat import ChatSessionMessage, ChatSessionMessageTimelineItem
from app.services.chat_session_service import ChatSessionService
from app.services.project_service import ProjectService

# ── Reuse fixture from test_chat_stream_relay.py ──

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
    """Return SSE-encoded bytes as manager_service.stream_chat yields them."""
    return [f'data: {json.dumps(p)}\n\n'.encode() for p in payloads]


class ChatStreamEndpointTest(unittest.TestCase):
    """U2, U3, U4, U8, U9 — Phase 1 /chat-stream endpoint tests."""

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

        # Create a real project via ProjectService (needs patched get_settings)
        # Use a unique project id per test to avoid cross-test collisions under xdist
        self.project_id = f"test-proj-{uuid4().hex[:8]}"
        self.project_service = ProjectService()
        self.project_service.create_project(self.project_id, "Test", "test goal")

        # Build TestClient with overridden dependencies
        self.client = TestClient(app)

        # Override dependencies for the test client
        app.dependency_overrides[get_project_service] = lambda: self.project_service

        # We need chat_session_service and chat_stream_relay to be real instances
        # but with patched manager_service.stream_chat
        from app.services.chat_stream_relay import ChatStreamRelay

        self.chat_session_service = ChatSessionService(self.project_service, manager_auto_service=None)
        self.manager_service_mock = MagicMock()

        self.chat_stream_relay = ChatStreamRelay(self.chat_session_service, self.manager_service_mock)

        app.dependency_overrides[get_chat_session_service] = lambda: self.chat_session_service
        app.dependency_overrides[get_chat_stream_relay] = lambda: self.chat_stream_relay
        app.dependency_overrides[get_manager_service] = lambda: self.manager_service_mock

        # Slash commands go through ManagerCommandService, which must publish to the
        # same ChatSessionService instance that tests subscribe to.
        from app.services.manager_command_service import ManagerCommandService
        self.manager_command_service = ManagerCommandService(MagicMock(), self.chat_session_service)
        app.dependency_overrides[get_manager_command_service] = lambda: self.manager_command_service

    def tearDown(self) -> None:
        self._chat_settings_patch.stop()
        self._settings_patch.stop()
        get_settings.cache_clear()
        get_project_service.cache_clear()
        get_chat_session_service.cache_clear()
        get_chat_stream_relay.cache_clear()
        get_manager_service.cache_clear()
        get_manager_command_service.cache_clear()
        app.dependency_overrides.clear()
        shutil.rmtree(self.data_root, ignore_errors=True)

    def _create_session(self, project_id: str | None = None) -> str:
        """Helper: create a chat session via POST and return session_id."""
        pid = project_id or self.project_id
        resp = self.client.post(f"/api/projects/{pid}/chat-sessions", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["session"]["session_id"]

    def _patch_stream_chat(self, payloads: list[dict]) -> None:
        """Helper: patch manager_service.stream_chat to return SSE bytes from payloads."""
        self.manager_service_mock.stream_chat.return_value = iter(payloads_to_sse_bytes(payloads))

    # ── U2 ────────────────────────────────────────────────────────────

    def test_chat_stream_writes_user_message(self) -> None:
        """U2: POST /chat-stream writes user + manager messages, session.revision > 0."""
        session_id = self._create_session()
        self._patch_stream_chat(EVENT_FIXTURE_PAYLOADS)

        resp = self.client.post(
            f"/api/projects/{self.project_id}/chat-stream",
            json={"message": "帮我看看这个项目", "session_id": session_id},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("x-blueprint-session-id"), session_id)

        # Consume the stream so relay finishes
        _ = resp.content

        # GET session and assert
        get_resp = self.client.get(f"/api/projects/{self.project_id}/chat-sessions/{session_id}")
        self.assertEqual(get_resp.status_code, 200)
        session = get_resp.json()["session"]

        messages = session["messages"]
        self.assertEqual(len(messages), 2, f"Expected 2 messages, got {len(messages)}")

        # First message: user
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "帮我看看这个项目")

        # Second message: manager
        self.assertEqual(messages[1]["role"], "manager")
        self.assertEqual(messages[1]["state"], "done")
        self.assertTrue(messages[1]["timeline"])
        self.assertGreater(session["revision"], 0)

    # ── U3 ────────────────────────────────────────────────────────────

    def test_chat_stream_no_frontend_put_needed(self) -> None:
        """U3: messages persist without any frontend PUT save_session."""
        session_id = self._create_session()
        self._patch_stream_chat(EVENT_FIXTURE_PAYLOADS)

        resp = self.client.post(
            f"/api/projects/{self.project_id}/chat-stream",
            json={"message": "hello", "session_id": session_id},
        )
        self.assertEqual(resp.status_code, 200)
        _ = resp.content  # consume stream

        # No PUT save_session called — just GET
        get_resp = self.client.get(f"/api/projects/{self.project_id}/chat-sessions/{session_id}")
        self.assertEqual(get_resp.status_code, 200)
        session = get_resp.json()["session"]

        messages = session["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "manager")
        self.assertEqual(messages[1]["state"], "done")
        self.assertTrue(messages[1]["timeline"])
        self.assertGreater(session["revision"], 0)

    # ── U4 ────────────────────────────────────────────────────────────

    def test_chat_stream_background_write_not_overwritten(self) -> None:
        """U4: depjob_terminal appended during stream is not overwritten by relay."""
        session_id = self._create_session()

        # Slow generator: yields one text_delta, sleeps, then yields response+done
        def slow_generator():
            yield f'data: {json.dumps({"type": "text_delta", "delta": "Working...", "assistant_turn_index": 0, "content_index": 1})}\n\n'.encode()
            time.sleep(0.5)
            yield f'data: {json.dumps({"type": "response", "response": {"message": "Done", "thinking": ""}})}\n\n'.encode()
            yield f'data: {json.dumps({"type": "done"})}\n\n'.encode()

        self.manager_service_mock.stream_chat.return_value = slow_generator()

        # Start POST in a thread so we can inject a background write while it runs
        post_result: dict = {"status_code": None, "content": None, "headers": None}

        def do_post():
            resp = self.client.post(
                f"/api/projects/{self.project_id}/chat-stream",
                json={"message": "trigger", "session_id": session_id},
            )
            post_result["status_code"] = resp.status_code
            post_result["content"] = resp.content
            post_result["headers"] = dict(resp.headers)

        post_thread = threading.Thread(target=do_post)
        post_thread.start()

        # Wait briefly for relay to start, then inject depjob_terminal
        time.sleep(0.1)
        self.chat_session_service.append_messages(
            self.project_id,
            session_id,
            [
                ChatSessionMessage(
                    id="depjob_terminal_001",
                    role="manager",
                    content="Dependency job finished.",
                    state="done",
                    timeline=[
                        ChatSessionMessageTimelineItem(
                            id="depjob_terminal_001_timeline",
                            kind="command",
                            content="job finished",
                            status="done",
                        ),
                    ],
                ),
            ],
        )

        post_thread.join(timeout=10)
        self.assertIsNotNone(post_result["status_code"])
        self.assertEqual(post_result["status_code"], 200)

        # GET session
        get_resp = self.client.get(f"/api/projects/{self.project_id}/chat-sessions/{session_id}")
        self.assertEqual(get_resp.status_code, 200)
        session = get_resp.json()["session"]
        messages = session["messages"]

        # Should have 3 messages: user, manager (from relay), depjob_terminal
        self.assertEqual(len(messages), 3, f"Expected 3 messages, got {len(messages)}: {[m['id'] for m in messages]}")

        ids = [m["id"] for m in messages]
        self.assertIn("depjob_terminal_001", ids)

        # Find the depjob_terminal message and verify it's intact
        depjob_msg = next((m for m in messages if m["id"] == "depjob_terminal_001"), None)
        self.assertIsNotNone(depjob_msg)
        self.assertEqual(depjob_msg["content"], "Dependency job finished.")

    # ── U8 ────────────────────────────────────────────────────────────

    def test_chat_stream_returns_session_id_header(self) -> None:
        """U8: POST without session_id creates a new session and returns it in header."""
        self._patch_stream_chat(EVENT_FIXTURE_PAYLOADS)

        resp = self.client.post(
            f"/api/projects/{self.project_id}/chat-stream",
            json={"message": "hi"},  # no session_id
        )
        self.assertEqual(resp.status_code, 200)

        session_id = resp.headers.get("x-blueprint-session-id")
        self.assertIsNotNone(session_id)
        self.assertTrue(session_id.startswith("session_"))

        # Consume stream
        _ = resp.content

        # GET that session should return 200
        get_resp = self.client.get(f"/api/projects/{self.project_id}/chat-sessions/{session_id}")
        self.assertEqual(get_resp.status_code, 200)
        session = get_resp.json()["session"]
        self.assertEqual(session["session_id"], session_id)

        # Should have 2 messages (user + manager)
        self.assertEqual(len(session["messages"]), 2)

    # ── U9 ────────────────────────────────────────────────────────────

    def test_chat_stream_client_disconnect_settles_message(self) -> None:
        """U9: client disconnects mid-stream; manager message settles (not running/thinking)."""
        session_id = self._create_session()

        # Slow generator: yields text_delta every 0.2s, no final done
        # This simulates a long-running stream that the client will abort
        def very_slow_generator():
            for i in range(20):
                yield f'data: {json.dumps({"type": "text_delta", "delta": f"chunk {i} ", "assistant_turn_index": 0, "content_index": 1})}\n\n'.encode()
                time.sleep(0.2)
            # No done event — stream just ends (or we simulate disconnect before this)

        self.manager_service_mock.stream_chat.return_value = very_slow_generator()

        # Use a low-level client to be able to disconnect mid-stream
        # TestClient doesn't support true disconnect well, so we use the internal
        # httpx client but close the response early.
        with self.client.stream(
            "POST",
            f"/api/projects/{self.project_id}/chat-stream",
            json={"message": "slow", "session_id": session_id},
        ) as resp:
            self.assertEqual(resp.status_code, 200)
            # Read only 2 chunks then close (disconnect)
            chunks = []
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                if len(chunks) >= 2:
                    break
            # Exiting the context manager closes the response

        # Wait for the relay to settle (generator exit handling)
        time.sleep(0.5)

        # GET session
        get_resp = self.client.get(f"/api/projects/{self.project_id}/chat-sessions/{session_id}")
        self.assertEqual(get_resp.status_code, 200)
        session = get_resp.json()["session"]
        messages = session["messages"]

        # Should have at least user + manager
        self.assertGreaterEqual(len(messages), 2)

        manager_msg = next((m for m in messages if m["role"] == "manager"), None)
        self.assertIsNotNone(manager_msg)

        # After a client-initiated disconnect the message should settle to done
        # (not error) and any in-flight timeline items should be interrupted.
        self.assertEqual(manager_msg["state"], "done",
                         f"Manager message state should be 'done' after disconnect, got {manager_msg['state']}")

        # Timeline should have no running items
        for item in manager_msg.get("timeline") or []:
            self.assertNotEqual(item.get("status"), "running",
                                f"Timeline item {item['id']} still running")
            self.assertNotEqual(item.get("status"), "error",
                                f"Timeline item {item['id']} should not be error after disconnect")

    # ── U10 ───────────────────────────────────────────────────────────

    def test_slash_command_publishes_message_upsert(self) -> None:
        """U10: slash command output is persisted and fans out message_upsert events."""
        session_id = self._create_session()

        event_iterator = self.chat_session_service.subscribe_events(
            self.project_id, session_id, timeout_seconds=0.5
        )

        resp = self.client.post(
            f"/api/projects/{self.project_id}/chat-stream",
            json={"message": "/auto status", "session_id": session_id},
        )
        self.assertEqual(resp.status_code, 200)
        _ = resp.content  # consume SSE response

        collected_events: list[dict] = []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                event = next(event_iterator)
                collected_events.append(event)
                if event.get("type") == "heartbeat":
                    break
            except StopIteration:
                break

        message_upserts = [e for e in collected_events if e.get("type") == "message_upsert"]
        self.assertTrue(message_upserts, "Should have received message_upsert events")

        upsert_ids = {e["message"]["id"] for e in message_upserts}
        self.assertTrue(
            any(mid.startswith("cmd_usr_") for mid in upsert_ids),
            "Expected a cmd_usr_* user command message in upserts",
        )
    # ── U11 ───────────────────────────────────────────────────────────

    def test_chat_compact_persists_summary_and_truncates(self) -> None:
        """U11: /chat-compact persists a compact message and truncates older messages."""
        session_id = self._create_session()
        # Seed the session with three messages; the oldest one will be truncated.
        self.chat_session_service.append_messages(
            self.project_id,
            session_id,
            [
                ChatSessionMessage(
                    id="msg_old",
                    role="user",
                    content="old",
                    state="done",
                    timeline=[
                        ChatSessionMessageTimelineItem(
                            id="msg_old_text",
                            kind="text",
                            content="old",
                            status="done",
                        ),
                    ],
                ),
                ChatSessionMessage(
                    id="msg_keep",
                    role="user",
                    content="keep me",
                    state="done",
                    timeline=[
                        ChatSessionMessageTimelineItem(
                            id="msg_keep_text",
                            kind="text",
                            content="keep me",
                            status="done",
                        ),
                    ],
                ),
                ChatSessionMessage(
                    id="msg_drop",
                    role="user",
                    content="drop me",
                    state="done",
                    timeline=[
                        ChatSessionMessageTimelineItem(
                            id="msg_drop_text",
                            kind="text",
                            content="drop me",
                            status="done",
                        ),
                    ],
                ),
            ],
        )

        self.manager_service_mock.compact_chat_session.return_value = {
            "compact_id": "compact_001",
            "summary": "压缩摘要",
            "first_kept_message_id": "msg_keep",
            "tokens_before": 1000,
            "tokens_after": 200,
            "duration_ms": 150,
            "provider": "test-provider",
            "model": "test-model",
        }

        resp = self.client.post(
            f"/api/projects/{self.project_id}/chat-compact",
            json={
                "message": "/compact",
                "session_id": session_id,
                "context": {},
                "thinking_effort": "medium",
                "messages": [],
                "session_messages": [],
            },
        )
        self.assertEqual(resp.status_code, 200)

        get_resp = self.client.get(f"/api/projects/{self.project_id}/chat-sessions/{session_id}")
        self.assertEqual(get_resp.status_code, 200)
        session = get_resp.json()["session"]
        messages = session["messages"]
        ids = [m["id"] for m in messages]

        self.assertIn("compact_msg_compact_001", ids)
        self.assertIn("msg_keep", ids)
        self.assertIn("msg_drop", ids)
        self.assertNotIn("msg_old", ids)

        compact_msg = next((m for m in messages if m["id"] == "compact_msg_compact_001"), None)
        self.assertIsNotNone(compact_msg)
        self.assertEqual(compact_msg["role"], "manager")
        self.assertEqual(compact_msg["content"], "压缩摘要")
        self.assertEqual(compact_msg["state"], "done")
        self.assertTrue(compact_msg["timeline"])
        self.assertEqual(compact_msg["timeline"][0]["kind"], "compact")


if __name__ == "__main__":
    unittest.main()
