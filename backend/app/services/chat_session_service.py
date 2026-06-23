from __future__ import annotations

from collections.abc import Iterator
import logging
from queue import Empty, Full, Queue
import re
import threading
from uuid import uuid4

from app.models.chat import ChatSession, ChatSessionMessage, ChatSessionSummary
from app.services.manager_auto_service import ManagerAutoService
from app.services.project_service import ProjectService
from app.services.utils import utc_now


logger = logging.getLogger(__name__)


class ChatSessionService:
    def __init__(self, project_service: ProjectService, manager_auto_service: ManagerAutoService | None = None) -> None:
        self.project_service = project_service
        self.manager_auto_service = manager_auto_service
        self._subscriber_lock = threading.Lock()
        self._subscribers: dict[tuple[str, str], list[Queue[dict]]] = {}

    def list_sessions(self, project_id: str) -> list[ChatSessionSummary]:
        sessions = self.project_service.graph_store(project_id).load_chat_sessions()
        auto_state = self.manager_auto_service.get_state(project_id) if self.manager_auto_service else None
        return [
            ChatSessionSummary(
                session_id=session.session_id,
                summary=session.summary,
                created_at=session.created_at,
                updated_at=session.updated_at,
                revision=session.revision,
                auto_owner=bool(auto_state and auto_state.enabled and auto_state.owner_session_id == session.session_id),
                auto_mode_state=(auto_state.state if auto_state and auto_state.enabled and auto_state.owner_session_id == session.session_id else None),
                btw_mode=bool(auto_state and auto_state.enabled and auto_state.owner_session_id and auto_state.owner_session_id != session.session_id),
                message_count=len(session.messages),
            )
            for session in sorted(sessions, key=lambda item: item.updated_at, reverse=True)
        ]

    def create_session(self, project_id: str, summary: str | None = None) -> ChatSession:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            sessions = store.load_chat_sessions()
            now = utc_now()
            session = ChatSession(
                session_id=f"session_{uuid4().hex[:12]}",
                summary=(summary or "新会话").strip() or "新会话",
                created_at=now,
                updated_at=now,
                revision=0,
                messages=[],
            )
            sessions.append(session)
            store.save_chat_sessions(sessions)
            return self._decorate_session(project_id, session)

    def get_session(self, project_id: str, session_id: str) -> ChatSession:
        sessions = self.project_service.graph_store(project_id).load_chat_sessions()
        session = next((item for item in sessions if item.session_id == session_id), None)
        if not session:
            raise ValueError(f"Chat session not found: {session_id}")
        return self._decorate_session(project_id, session)

    def save_session(
        self,
        project_id: str,
        session_id: str,
        messages: list[ChatSessionMessage],
        summary: str | None = None,
        base_revision: int | None = None,
    ) -> ChatSession:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            sessions = store.load_chat_sessions()
            session = next((item for item in sessions if item.session_id == session_id), None)
            if not session:
                raise ValueError(f"Chat session not found: {session_id}")
            session.messages = self._merge_messages(session.messages, messages) if base_revision is not None and base_revision != session.revision else messages
            session.summary = self._derive_summary(session.messages, summary or session.summary)
            session.updated_at = utc_now()
            session.revision += 1
            store.save_chat_sessions(sessions)
            self._publish_session_event(project_id, session_id, {"type": "session_saved", "session": session.model_dump()})
            return self._decorate_session(project_id, session)

    def append_messages(
        self,
        project_id: str,
        session_id: str,
        messages: list[ChatSessionMessage],
        dedupe_ids: list[str] | None = None,
    ) -> ChatSession:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            sessions = store.load_chat_sessions()
            session = next((item for item in sessions if item.session_id == session_id), None)
            if not session:
                raise ValueError(f"Chat session not found: {session_id}")
            existing_ids = {item.id for item in session.messages}
            dedupe = set(dedupe_ids or [])
            appended = False
            for message in messages:
                if message.id in existing_ids or message.id in dedupe:
                    continue
                session.messages.append(message)
                existing_ids.add(message.id)
                appended = True
            if appended:
                session.summary = self._derive_summary(session.messages, session.summary)
                session.updated_at = utc_now()
                session.revision += 1
                store.save_chat_sessions(sessions)
                for message in messages:
                    self._publish_session_event(
                        project_id,
                        session_id,
                        {
                            "type": "message_upsert",
                            "message": message.model_dump(),
                            "revision": session.revision,
                        },
                    )
            return self._decorate_session(project_id, session)

    def upsert_message(self, project_id: str, session_id: str, message: ChatSessionMessage) -> ChatSession:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            sessions = store.load_chat_sessions()
            session = next((item for item in sessions if item.session_id == session_id), None)
            if not session:
                raise ValueError(f"Chat session not found: {session_id}")
            for index, existing in enumerate(session.messages):
                if existing.id == message.id:
                    session.messages[index] = message
                    break
            else:
                session.messages.append(message)
            session.summary = self._derive_summary(session.messages, session.summary)
            session.updated_at = utc_now()
            session.revision += 1
            store.save_chat_sessions(sessions)
            self._publish_session_event(
                project_id,
                session_id,
                {
                    "type": "message_upsert",
                    "message": message.model_dump(),
                    "revision": session.revision,
                },
            )
            return self._decorate_session(project_id, session)

    def subscribe_events(
        self,
        project_id: str,
        session_id: str,
        *,
        timeout_seconds: float = 15.0,
    ) -> Iterator[dict]:
        queue: Queue[dict] = Queue(maxsize=256)
        key = (project_id, session_id)
        # Register the queue eagerly so that callers can subscribe before triggering
        # the publisher and still receive all events.
        with self._subscriber_lock:
            self._subscribers.setdefault(key, []).append(queue)

        def _iter() -> Iterator[dict]:
            try:
                while True:
                    try:
                        yield queue.get(timeout=timeout_seconds)
                    except Empty:
                        yield {"type": "heartbeat"}
            finally:
                with self._subscriber_lock:
                    subscribers = self._subscribers.get(key, [])
                    if queue in subscribers:
                        subscribers.remove(queue)
                    if not subscribers:
                        self._subscribers.pop(key, None)

        return _iter()

    def publish_stream_event(
        self,
        project_id: str,
        session_id: str,
        *,
        message_id: str,
        event: dict,
        revision: int | None = None,
        seq: int | None = None,
    ) -> None:
        payload = {
            "type": "stream_event",
            "message_id": message_id,
            "event": event,
            "revision": revision,
        }
        if seq is not None:
            payload["seq"] = seq
        self._publish_session_event(project_id, session_id, payload)

    def delete_session(self, project_id: str, session_id: str) -> None:
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            sessions = store.load_chat_sessions()
            remaining = [item for item in sessions if item.session_id != session_id]
            if len(remaining) == len(sessions):
                raise ValueError(f"Chat session not found: {session_id}")
            store.save_chat_sessions(remaining)

    @staticmethod
    def _derive_summary(messages: list[ChatSessionMessage], fallback: str) -> str:
        first_user_message = next((item.content for item in messages if item.role == "user" and item.content.strip()), "")
        if not first_user_message:
            return fallback.strip() or "新会话"
        compact = re.sub(r"\s+", " ", first_user_message).strip()
        return compact[:40] + ("…" if len(compact) > 40 else "")

    @staticmethod
    def _merge_messages(existing: list[ChatSessionMessage], incoming: list[ChatSessionMessage]) -> list[ChatSessionMessage]:
        merged = {item.id: item for item in existing}
        order = [item.id for item in existing]
        for item in incoming:
            if item.id not in merged:
                order.append(item.id)
            merged[item.id] = item
        return [merged[item_id] for item_id in order if item_id in merged]

    def _decorate_session(self, project_id: str, session: ChatSession) -> ChatSession:
        if self.manager_auto_service is None:
            return session
        auto_state = self.manager_auto_service.get_state(project_id)
        decorated = session.model_copy(deep=True)
        decorated.auto_owner = bool(auto_state.enabled and auto_state.owner_session_id == session.session_id)
        decorated.auto_mode_state = auto_state.state if decorated.auto_owner and auto_state.enabled else None
        decorated.btw_mode = bool(auto_state.enabled and auto_state.owner_session_id and auto_state.owner_session_id != session.session_id)
        return decorated

    def _publish_session_event(self, project_id: str, session_id: str, payload: dict) -> None:
        key = (project_id, session_id)
        with self._subscriber_lock:
            subscribers = list(self._subscribers.get(key, []))
        for queue in subscribers:
            try:
                queue.put_nowait(payload)
            except Full:
                logger.warning(
                    "Dropping chat session event because subscriber queue is full: project_id=%s session_id=%s event_type=%s",
                    project_id,
                    session_id,
                    payload.get("type"),
                )
