import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services.utils import parse_slash_command
from app.core.config import get_settings
from app.main import app
from app.models.chat import ChatRequest, ChatHistoryMessage, ChatSessionMessage, ChatSessionMessageTimelineItem
from app.services.manager_service import ManagerService
from app.services.project_service import ProjectService

class AutoCommandInterceptionTest(unittest.TestCase):
    def test_parse_slash_command(self):
        # 1. bare /auto
        is_cmd, cmd_type, obj = parse_slash_command("/auto")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "bare")
        self.assertIsNone(obj)

        is_cmd, cmd_type, obj = parse_slash_command("  /auto  ")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "bare")
        self.assertIsNone(obj)

        # 2. subcommands
        is_cmd, cmd_type, obj = parse_slash_command("/auto off")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "stop")
        self.assertEqual(obj, "off")

        is_cmd, cmd_type, obj = parse_slash_command("/auto stop")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "stop")
        self.assertEqual(obj, "stop")

        is_cmd, cmd_type, obj = parse_slash_command("/auto status")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "status")
        self.assertEqual(obj, "status")

        is_cmd, cmd_type, obj = parse_slash_command("/auto once")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "once")
        self.assertEqual(obj, "once")

        # 3. enable /auto <objective>
        is_cmd, cmd_type, obj = parse_slash_command("/auto 启动分析吧")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "enable")
        self.assertEqual(obj, "启动分析吧")

        is_cmd, cmd_type, obj = parse_slash_command("/auto   继续推进 ")
        self.assertTrue(is_cmd)
        self.assertEqual(cmd_type, "enable")
        self.assertEqual(obj, "继续推进")

        # 4. non-matching
        is_cmd, cmd_type, obj = parse_slash_command("/auto@目标")
        self.assertFalse(is_cmd)

        is_cmd, cmd_type, obj = parse_slash_command("/Auto 继续推进")
        self.assertFalse(is_cmd)

        is_cmd, cmd_type, obj = parse_slash_command("hello world")
        self.assertFalse(is_cmd)

        is_cmd, cmd_type, obj = parse_slash_command("/auto 第一行\n第二行")
        self.assertFalse(is_cmd)

        is_cmd, cmd_type, obj = parse_slash_command("/auto\n继续推进")
        self.assertFalse(is_cmd)

    def test_sanitize_chat_request_messages(self):
        from app.core.config import get_settings
        # We'll mock the minimal ProjectService needed to construct ManagerService
        class DummyProjectService:
            settings = get_settings()

        manager_service = ManagerService(project_service=DummyProjectService()) # type: ignore

        chat_request = ChatRequest(
            message="Let's continue",
            session_id="session_1",
            messages=[
                ChatHistoryMessage(role="user", content="hello"),
                ChatHistoryMessage(role="user", content="/auto 启动分析吧"),
                ChatHistoryMessage(role="manager", content="已允许当前会话继续消费 workboard"),
                ChatHistoryMessage(role="user", content="another normal message"),
            ],
            session_messages=[
                ChatSessionMessage(id="msg_1", role="user", content="hello", state="done"),
                ChatSessionMessage(
                    id="cmd_usr_1234",
                    role="user",
                    content="/auto 启动分析吧",
                    state="done",
                    timeline=[ChatSessionMessageTimelineItem(id="item_1", kind="command", status="done")]
                ),
                ChatSessionMessage(
                    id="cmd_mgr_5678",
                    role="manager",
                    content="已允许当前会话继续消费 workboard",
                    state="done",
                    timeline=[ChatSessionMessageTimelineItem(id="item_2", kind="command", status="done")]
                ),
                ChatSessionMessage(id="msg_2", role="user", content="another normal message", state="done"),
            ]
        )

        manager_service._sanitize_chat_request_messages(chat_request)

        # Verify that command messages were stripped from messages
        self.assertEqual(len(chat_request.messages), 2)
        self.assertEqual(chat_request.messages[0].content, "hello")
        self.assertEqual(chat_request.messages[1].content, "another normal message")

        # Verify that command messages were stripped from session_messages
        self.assertEqual(len(chat_request.session_messages), 2)
        self.assertEqual(chat_request.session_messages[0].id, "msg_1")
        self.assertEqual(chat_request.session_messages[1].id, "msg_2")


class AutoCommandIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="blueprint-re-auto-cmd-test-")
        self.settings = get_settings()
        self._original_data_root = self.settings.data_root
        self.settings.data_root = Path(self.tmpdir)
        self.project_service = ProjectService()
        self.project_service.create_project(
            project_id="test-project",
            name="Test Project",
            current_goal="E2E test auto commands",
            seed_demo=False,
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.settings.data_root = self._original_data_root
        # Clear cached FastAPI dependency factories so later tests start with the correct settings.
        from app.api.deps import (
            get_app_config_service,
            get_chat_session_service,
            get_diagnostic_bundle_service,
            get_manager_auto_service,
            get_manager_command_service,
            get_project_service,
            get_runtime_dependency_job_service,
            get_worker_service,
        )
        from app.core.config import get_settings

        get_settings.cache_clear()
        get_project_service.cache_clear()
        get_app_config_service.cache_clear()
        get_worker_service.cache_clear()
        get_manager_auto_service.cache_clear()
        get_chat_session_service.cache_clear()
        get_manager_command_service.cache_clear()
        get_runtime_dependency_job_service.cache_clear()
        get_diagnostic_bundle_service.cache_clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mock_stream_chat(self, project_id, chat_request):
        """Return a mock SSE stream that looks like a normal Manager chat response."""
        def gen():
            yield b'data: {"type":"text_delta","delta":"mock normal chat response"}\n\n'
            yield b'data: {"type":"done"}\n\n'
        return gen()

    def test_e2e_chat_stream_auto_enable_and_stop(self):
        # 1. Create a chat session first (needed for bypass tests to use real session)
        response = self.client.post("/api/projects/test-project/chat-sessions", json={"summary": "Session 1"})
        self.assertEqual(response.status_code, 200)
        session_id = response.json()["session"]["session_id"]

        # 0. Verify that a non-matching command-like text (such as /auto@目标 or a multiline command)
        # is treated as a normal chat and bypasses operational command stream interception.
        # We mock ManagerService.stream_chat to prove the request reaches normal chat relay.
        with patch.object(ManagerService, "stream_chat", self._mock_stream_chat):
            # 0a. /auto@目标 should bypass command interception and hit normal chat
            chat_request_non_cmd = {
                "message": "/auto@目标",
                "message_id": "non_cmd_usr_test",
                "session_id": session_id,
                "messages": [],
                "session_messages": []
            }
            response_non_cmd = self.client.post("/api/projects/test-project/chat-stream", json=chat_request_non_cmd)
            self.assertEqual(response_non_cmd.status_code, 200)
            sse_lines_non_cmd = response_non_cmd.text.split("\n")
            events_non_cmd = []
            for line in sse_lines_non_cmd:
                if line.startswith("data: "):
                    try:
                        events_non_cmd.append(json.loads(line[6:].strip()))
                    except Exception:
                        pass
            # Must receive the mock normal chat response, proving it went through stream_chat
            self.assertTrue(any(ev.get("type") == "text_delta" and "mock normal chat response" in ev.get("delta", "") for ev in events_non_cmd))
            # Must NOT receive an auto command ack
            self.assertFalse(any("已允许当前会话继续消费" in ev.get("delta", "") for ev in events_non_cmd if ev.get("type") == "text_delta"))
            # Must NOT receive an error event (would indicate session-missing or other guard)
            self.assertFalse(any(ev.get("type") == "error" for ev in events_non_cmd))

            # 0b. Multiline /auto should also bypass command interception
            chat_request_multiline = {
                "message": "/auto 第一行\n第二行",
                "message_id": "non_cmd_usr_multiline",
                "session_id": session_id,
                "messages": [],
                "session_messages": []
            }
            response_multiline = self.client.post("/api/projects/test-project/chat-stream", json=chat_request_multiline)
            self.assertEqual(response_multiline.status_code, 200)
            sse_lines_multiline = response_multiline.text.split("\n")
            events_multiline = []
            for line in sse_lines_multiline:
                if line.startswith("data: "):
                    try:
                        events_multiline.append(json.loads(line[6:].strip()))
                    except Exception:
                        pass
            self.assertTrue(any(ev.get("type") == "text_delta" and "mock normal chat response" in ev.get("delta", "") for ev in events_multiline))
            self.assertFalse(any("已允许当前会话继续消费" in ev.get("delta", "") for ev in events_multiline if ev.get("type") == "text_delta"))
            self.assertFalse(any(ev.get("type") == "error" for ev in events_multiline))

        # 2. Trigger /auto 启动分析吧
        chat_request = {
            "message": "/auto 启动分析吧",
            "message_id": "cmd_usr_test123",
            "session_id": session_id,
            "messages": [],
            "session_messages": []
        }
        
        # We perform POST request to chat-stream
        response = self.client.post("/api/projects/test-project/chat-stream", json=chat_request)
        self.assertEqual(response.status_code, 200)
        
        # Verify SSE stream contents
        sse_lines = response.text.split("\n")
        events = []
        for line in sse_lines:
            if line.startswith("data: "):
                data_str = line[len("data: "):].strip()
                events.append(json.loads(data_str))

        self.assertTrue(any(ev["type"] == "text_delta" and "已允许当前会话继续消费 workboard" in ev.get("delta", "") for ev in events))
        self.assertTrue(any(ev["type"] == "response" and "已允许当前会话继续消费 workboard" in ev.get("response", {}).get("message", "") for ev in events))
        self.assertTrue(any(ev["type"] == "done" for ev in events))

        # Verify DB persistence
        get_sess_resp = self.client.get(f"/api/projects/test-project/chat-sessions/{session_id}")
        self.assertEqual(get_sess_resp.status_code, 200)
        session_data = get_sess_resp.json()["session"]
        messages = session_data["messages"]
        self.assertEqual(len(messages), 2)
        
        user_msg = messages[0]
        self.assertEqual(user_msg["id"], "cmd_usr_test123")
        self.assertEqual(user_msg["role"], "user")
        self.assertEqual(user_msg["content"], "/auto 启动分析吧")
        self.assertEqual(user_msg["timeline"][0]["kind"], "command")

        mgr_msg = messages[1]
        self.assertEqual(mgr_msg["id"], "cmd_mgr_test123")
        self.assertEqual(mgr_msg["role"], "manager")
        self.assertTrue("已允许当前会话继续消费 workboard" in mgr_msg["content"])
        self.assertEqual(mgr_msg["timeline"][0]["kind"], "command")

        # Verify project auto state
        auto_state_resp = self.client.get("/api/projects/test-project/manager-auto")
        self.assertEqual(auto_state_resp.status_code, 200)
        state_data = auto_state_resp.json()["state"]
        self.assertTrue(state_data["enabled"])
        self.assertEqual(state_data["scope_objective"], "启动分析吧")
        self.assertEqual(state_data["owner_session_id"], session_id)

        # 3. Create another session
        response_2 = self.client.post("/api/projects/test-project/chat-sessions", json={"summary": "Session 2"})
        session_id_2 = response_2.json()["session"]["session_id"]

        # Try to /auto stop from session 2 (unauthorized stop)
        chat_request_stop_unauth = {
            "message": "/auto stop",
            "message_id": "cmd_usr_unauthstop",
            "session_id": session_id_2,
            "messages": [],
            "session_messages": []
        }
        response_stop_unauth = self.client.post("/api/projects/test-project/chat-stream", json=chat_request_stop_unauth)
        # Should persist the error in session 2 database and return error event
        sse_lines_unauth = response_stop_unauth.text.split("\n")
        events_unauth = []
        for line in sse_lines_unauth:
            if line.startswith("data: "):
                data_str = line[len("data: "):].strip()
                events_unauth.append(json.loads(data_str))

        self.assertTrue(any(ev["type"] == "error" and "Only the auto owner session may stop auto mode" in ev.get("detail", "") for ev in events_unauth))

        # Check DB of Session 2 to make sure both user unauth stop attempt and manager error response are persisted
        get_sess_resp_2 = self.client.get(f"/api/projects/test-project/chat-sessions/{session_id_2}")
        session_data_2 = get_sess_resp_2.json()["session"]
        messages_2 = session_data_2["messages"]
        self.assertEqual(len(messages_2), 2)
        self.assertEqual(messages_2[0]["id"], "cmd_usr_unauthstop")
        self.assertEqual(messages_2[1]["id"], "cmd_mgr_unauthstop")
        self.assertEqual(messages_2[1]["state"], "error")
        self.assertTrue("Only the auto owner session may stop auto mode" in messages_2[1]["content"])

        # Stop auto from Session 1 (owner stop)
        chat_request_stop_auth = {
            "message": "/auto stop",
            "message_id": "cmd_usr_stop123",
            "session_id": session_id,
            "messages": [],
            "session_messages": []
        }
        response_stop_auth = self.client.post("/api/projects/test-project/chat-stream", json=chat_request_stop_auth)
        self.assertEqual(response_stop_auth.status_code, 200)

        # Check DB of Session 1 to verify stop command and manager stop ack are persisted
        get_sess_resp_1_after = self.client.get(f"/api/projects/test-project/chat-sessions/{session_id}")
        session_data_1_after = get_sess_resp_1_after.json()["session"]
        # Session 1 messages should now include the initial auto, initial manager ack, stop attempt, and stop manager ack.
        self.assertEqual(len(session_data_1_after["messages"]), 4)
        self.assertEqual(session_data_1_after["messages"][2]["content"], "/auto stop")
        self.assertTrue("因用户停止任务，已退出 auto 模式" in session_data_1_after["messages"][3]["content"])

        # Verify project auto state is now disabled
        auto_state_resp_after = self.client.get("/api/projects/test-project/manager-auto")
        self.assertFalse(auto_state_resp_after.json()["state"]["enabled"])

    def test_error_command_timeline_merging_autosave(self):
        # 1. Create a chat session
        response = self.client.post("/api/projects/test-project/chat-sessions", json={"summary": "Session 1"})
        self.assertEqual(response.status_code, 200)
        session_data = response.json()["session"]
        session_id = session_data["session_id"]
        revision = session_data["revision"]

        # 2. Simulate the backend saving a command error message.
        # This writes a timeline item of kind "command".
        user_message_id = "cmd_usr_errtest"
        manager_message_id = "cmd_mgr_errtest"

        messages_payload = [
            {
                "id": user_message_id,
                "role": "user",
                "content": "/auto 启动吧",
                "state": "done",
                "timeline": [
                    {
                        "id": f"{user_message_id}_text",
                        "kind": "command",
                        "content": "/auto 启动吧",
                        "status": "done"
                    }
                ]
            },
            {
                "id": manager_message_id,
                "role": "manager",
                "content": "some error details",
                "state": "error",
                "timeline": [
                    {
                        "id": f"{manager_message_id}_text",
                        "kind": "command",
                        "content": "some error details",
                        "status": "error"
                    }
                ]
            }
        ]

        # 3. Simulate frontend saving the chat session (autosave merge).
        # We PUT back to save the session.
        save_resp = self.client.put(
            f"/api/projects/test-project/chat-sessions/{session_id}",
            json={
                "messages": messages_payload,
                "summary": "Merged session",
                "base_revision": revision
            }
        )
        self.assertEqual(save_resp.status_code, 200)

        # 4. Fetch the session and verify that the command timeline items are perfectly preserved.
        get_resp = self.client.get(f"/api/projects/test-project/chat-sessions/{session_id}")
        self.assertEqual(get_resp.status_code, 200)
        fetched_session = get_resp.json()["session"]
        fetched_messages = fetched_session["messages"]

        self.assertEqual(len(fetched_messages), 2)
        # Verify user message command timeline
        self.assertEqual(fetched_messages[0]["id"], user_message_id)
        self.assertEqual(fetched_messages[0]["timeline"][0]["kind"], "command")
        # Verify manager message command timeline
        self.assertEqual(fetched_messages[1]["id"], manager_message_id)
        self.assertEqual(fetched_messages[1]["state"], "error")
        self.assertEqual(fetched_messages[1]["timeline"][0]["kind"], "command")
        self.assertEqual(fetched_messages[1]["timeline"][0]["status"], "error")


if __name__ == "__main__":
    import tempfile
    import shutil
    from pathlib import Path
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.config import get_settings
    from app.services.project_service import ProjectService
    unittest.main()
