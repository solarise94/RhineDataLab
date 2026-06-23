from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any


class StreamInterrupted(BaseException):
    """Raised when an HTTP client disconnects mid-stream."""

from app.models.chat import ChatRequest, ChatResponse, ChatSessionMessage, ChatSessionMessageTimelineItem, ChatTokenUsage
from app.models.patches import Proposal
from app.services.chat_session_service import ChatSessionService
from app.services.chat_stream_events import iter_sse_payloads
from app.services.manager_service import ManagerService


STREAM_EVENT_PUBLISH_INTERVAL_SECONDS = 0.125
STREAM_EVENT_PERSIST_INTERVAL_SECONDS = 0.75


class ChatStreamRelay:
    """Relay one Manager stream into the canonical chat-session stream model."""

    def __init__(self, chat_session_service: ChatSessionService, manager_service: ManagerService) -> None:
        self.chat_session_service = chat_session_service
        self.manager_service = manager_service

    def run_to_session(
        self,
        project_id: str,
        session_id: str,
        request: ChatRequest,
        *,
        message_id: str,
        initial_thinking: str | None = None,
    ) -> ChatResponse:
        message = self._initial_stream_message(message_id, initial_thinking)
        self.chat_session_service.upsert_message(project_id, session_id, message)
        saw_response = False
        last_persisted_at = time.monotonic()
        last_published_at = last_persisted_at
        pending_stream_payloads: list[dict[str, Any]] = []
        stream_seq = 0

        def flush_pending_stream_payloads(now: float) -> None:
            nonlocal last_published_at, stream_seq
            if not pending_stream_payloads:
                return
            for pending_payload in pending_stream_payloads:
                stream_seq += 1
                self.chat_session_service.publish_stream_event(
                    project_id,
                    session_id,
                    message_id=message.id,
                    event=pending_payload,
                    seq=stream_seq,
                )
            pending_stream_payloads.clear()
            last_published_at = now

        try:
            for payload in self._iter_stream_payloads(project_id, request):
                message = self._apply_stream_payload(message, payload)
                event_type = payload.get("type")
                if event_type == "response":
                    saw_response = True
                now = time.monotonic()
                if self._is_immediate_stream_event(event_type):
                    flush_pending_stream_payloads(now)
                    stream_seq += 1
                    self.chat_session_service.publish_stream_event(
                        project_id,
                        session_id,
                        message_id=message.id,
                        event=payload,
                        seq=stream_seq,
                    )
                    last_published_at = now
                else:
                    pending_stream_payloads.append(payload)
                    if now - last_published_at >= STREAM_EVENT_PUBLISH_INTERVAL_SECONDS:
                        flush_pending_stream_payloads(now)
                if self._should_persist_stream_event(event_type, now, last_persisted_at):
                    self.chat_session_service.upsert_message(project_id, session_id, message)
                    last_persisted_at = now
                if event_type == "error":
                    raise RuntimeError(str(payload.get("detail") or "Manager stream failed."))
            flush_pending_stream_payloads(time.monotonic())
            if message.state not in {"done", "error"}:
                message = self.settle_stream_message(message, "done")
            if not saw_response and not message.content.strip():
                raise RuntimeError("Manager stream ended without a response.")
            self.chat_session_service.upsert_message(project_id, session_id, message)
            return ChatResponse(message=message.content, thinking=message.thinking, metadata={"token_usage": message.token_usage.model_dump() if message.token_usage else None})
        except Exception:
            message = self.settle_stream_message(message, "error")
            if not message.content:
                message.content = "AUTO 处理意外中断。"
            self.chat_session_service.upsert_message(project_id, session_id, message)
            raise

    def stream_to_http(
        self,
        project_id: str,
        session_id: str,
        request: ChatRequest,
        *,
        message_id: str,
        initial_thinking: str | None = None,
        disconnect_event: threading.Event | None = None,
    ) -> Iterator[bytes]:
        """Mirror run_to_session logic, but yield SSE bytes to HTTP consumers.

        Persists via chat_session_service.upsert_message and fans out via
        chat_session_service.publish_stream_event exactly as run_to_session does.
        Yields ``data: {json.dumps(payload)}\n\n`` for every published stream_event
        (including batched ones) and a final ``message_upsert`` event after the last
        upsert_message so HTTP consumers see the authoritative snapshot.

        On a stream error event or unexpected exception, settles the message to
        ``error``, persists it, and yields an ``error`` SSE event. If the HTTP client
        disconnects (``GeneratorExit`` or ``disconnect_event``), the message and any
        running timeline items are settled to ``interrupted`` (message state becomes
        ``done``) and the exception is re-raised so the generator closes cleanly.
        """
        message = self._initial_stream_message(message_id, initial_thinking)
        self.chat_session_service.upsert_message(project_id, session_id, message)
        saw_response = False
        last_persisted_at = time.monotonic()
        last_published_at = last_persisted_at
        pending_stream_payloads: list[dict[str, Any]] = []
        stream_seq = 0
        yielded_error = False

        def _yield_event(payload: dict[str, Any]) -> bytes:
            return f"data: {json.dumps(payload)}\n\n".encode()

        def flush_pending_stream_payloads(now: float) -> Iterator[bytes]:
            nonlocal last_published_at, stream_seq
            if not pending_stream_payloads:
                return
            for pending_payload in pending_stream_payloads:
                stream_seq += 1
                self.chat_session_service.publish_stream_event(
                    project_id,
                    session_id,
                    message_id=message.id,
                    event=pending_payload,
                    seq=stream_seq,
                )
                yield _yield_event(pending_payload)
            pending_stream_payloads.clear()
            last_published_at = now

        def _check_disconnect() -> None:
            if disconnect_event is not None and disconnect_event.is_set():
                raise StreamInterrupted()

        try:
            for payload in self._iter_stream_payloads(project_id, request):
                _check_disconnect()
                message = self._apply_stream_payload(message, payload)
                event_type = payload.get("type")
                if event_type == "response":
                    saw_response = True
                now = time.monotonic()
                if self._is_immediate_stream_event(event_type):
                    yield from flush_pending_stream_payloads(now)
                    stream_seq += 1
                    self.chat_session_service.publish_stream_event(
                        project_id,
                        session_id,
                        message_id=message.id,
                        event=payload,
                        seq=stream_seq,
                    )
                    yield _yield_event(payload)
                    last_published_at = now
                else:
                    pending_stream_payloads.append(payload)
                    if now - last_published_at >= STREAM_EVENT_PUBLISH_INTERVAL_SECONDS:
                        yield from flush_pending_stream_payloads(now)
                if self._should_persist_stream_event(event_type, now, last_persisted_at):
                    self.chat_session_service.upsert_message(project_id, session_id, message)
                    last_persisted_at = now
                if event_type == "error":
                    raise RuntimeError(str(payload.get("detail") or "Manager stream failed."))
                _check_disconnect()
            yield from flush_pending_stream_payloads(time.monotonic())
            if message.state not in {"done", "error"}:
                message = self.settle_stream_message(message, "done")
            if not saw_response and not message.content.strip():
                raise RuntimeError("Manager stream ended without a response.")
            self.chat_session_service.upsert_message(project_id, session_id, message)
            yield _yield_event({"type": "message_upsert", "message": message.model_dump()})
            return
        except (GeneratorExit, StreamInterrupted):
            message = self.settle_stream_message(message, "interrupted")
            if not message.content:
                message.content = "生成已停止。"
            self.chat_session_service.upsert_message(project_id, session_id, message)
            raise
        except Exception:
            message = self.settle_stream_message(message, "error")
            if not message.content:
                message.content = "AUTO 处理意外中断。"
            self.chat_session_service.upsert_message(project_id, session_id, message)
            if not yielded_error:
                yield _yield_event({"type": "error", "detail": message.content})
                yielded_error = True
            return

    def settle_message(self, project_id: str, session_id: str, message_id: str, status: str) -> None:
        session = self.chat_session_service.get_session(project_id, session_id)
        message = next((item for item in session.messages if item.id == message_id), None)
        if message and message.state not in {"done", "error"}:
            self.chat_session_service.upsert_message(project_id, session_id, self.settle_stream_message(message, status))

    @staticmethod
    def _is_immediate_stream_event(event_type: object) -> bool:
        return event_type in {
            "thinking_start",
            "thinking_end",
            "tool_start",
            "tool_end",
            "tool_report",
            "proposal",
            "response",
            "done",
            "error",
            "usage",
        }

    @staticmethod
    def _should_persist_stream_event(event_type: object, now: float, last_persisted_at: float) -> bool:
        if event_type in {
            "thinking_start",
            "thinking_end",
            "tool_start",
            "tool_end",
            "tool_report",
            "proposal",
            "response",
            "done",
            "error",
            "usage",
        }:
            return True
        return now - last_persisted_at >= STREAM_EVENT_PERSIST_INTERVAL_SECONDS

    def _iter_stream_payloads(self, project_id: str, request: ChatRequest) -> Iterator[dict[str, Any]]:
        return iter_sse_payloads(self.manager_service.stream_chat(project_id, request), invalid_json_message="Invalid Manager stream event")

    def _initial_stream_message(self, message_id: str, initial_thinking: str | None) -> ChatSessionMessage:
        if initial_thinking:
            return ChatSessionMessage(
                id=message_id,
                role="manager",
                content="",
                thinking=initial_thinking,
                state="thinking",
                timeline=[
                    ChatSessionMessageTimelineItem(
                        id=f"{message_id}_thinking_hb",
                        kind="thinking",
                        content=initial_thinking,
                        status="running",
                        started_at=self._now_ms(),
                    )
                ],
            )
        return ChatSessionMessage(id=message_id, role="manager", content="", state="thinking", timeline=[])

    def _apply_stream_payload(self, message: ChatSessionMessage, payload: dict[str, Any]) -> ChatSessionMessage:
        event_type = str(payload.get("type") or "")
        next_message = message.model_copy(deep=True)
        timeline = list(next_message.timeline or [])
        if event_type == "thinking_start":
            item_id = self._timeline_item_id("thinking", payload, str(len(timeline)))
            timeline = [item for item in timeline if not item.id.endswith("_thinking_hb")]
            existing = self._find_timeline_item(timeline, item_id)
            timeline = self._upsert_timeline_item(
                timeline,
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="thinking",
                    content=existing.content if existing else "",
                    status="running",
                    started_at=existing.started_at if existing else self._event_number(payload, "started_at") or self._now_ms(),
                ),
            )
            next_message.state = "streaming" if next_message.content else "thinking"
        elif event_type == "thinking_delta":
            item_id = self._timeline_item_id("thinking", payload, self._running_item_id(timeline, "thinking") or str(len(timeline)))
            delta = str(payload.get("delta") or "")
            existing = self._find_timeline_item(timeline, item_id)
            content = f"{existing.content if existing and existing.content else ''}{delta}"
            timeline = self._upsert_timeline_item(
                timeline,
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="thinking",
                    content=content,
                    status="running",
                    started_at=existing.started_at if existing else self._now_ms(),
                ),
            )
            next_message.thinking = f"{next_message.thinking or ''}{delta}"
            next_message.state = "streaming" if next_message.content else "thinking"
        elif event_type == "thinking_end":
            item_id = self._timeline_item_id("thinking", payload, self._running_item_id(timeline, "thinking") or str(len(timeline)))
            existing = self._find_timeline_item(timeline, item_id)
            content = str(payload.get("content") or (existing.content if existing else "") or next_message.thinking or "")
            timeline = self._upsert_timeline_item(
                timeline,
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="thinking",
                    content=content,
                    status="done",
                    started_at=(existing.started_at if existing else self._event_number(payload, "started_at")) or self._now_ms(),
                    ended_at=self._event_number(payload, "ended_at") or self._now_ms(),
                ),
            )
            if content:
                next_message.thinking = content
        elif event_type == "heartbeat":
            content = next_message.thinking or str(payload.get("message") or "Manager 正在生成回复…")
            running_id = self._running_item_id(timeline, "thinking")
            item_id = running_id or f"{next_message.id}_thinking_hb"
            existing = self._find_timeline_item(timeline, item_id)
            timeline = self._upsert_timeline_item(
                timeline,
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="thinking",
                    content=content,
                    status="running",
                    started_at=existing.started_at if existing else self._now_ms(),
                ),
            )
            next_message.thinking = content
            next_message.state = "streaming" if next_message.content else "thinking"
        elif event_type == "text_delta":
            item_id = self._timeline_item_id("text", payload, self._last_text_id(timeline) or str(len(timeline)))
            delta = str(payload.get("delta") or "")
            existing = self._find_timeline_item(timeline, item_id)
            timeline = self._upsert_timeline_item(
                self._settle_running_timeline(timeline, "text", "done", exclude_id=item_id),
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="text",
                    content=f"{existing.content if existing and existing.content else ''}{delta}",
                    status="running",
                ),
            )
            next_message.content = f"{next_message.content}{delta}"
            next_message.state = "streaming"
        elif event_type == "usage":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                next_message.token_usage = ChatTokenUsage.model_validate(usage)
        elif event_type == "tool_start":
            item_id = str(payload.get("tool_call_id") or self._timeline_item_id("tool", payload, f"{payload.get('tool_name') or 'unknown'}_{len(timeline)}"))
            existing = self._find_timeline_item(timeline, item_id)
            timeline = self._upsert_timeline_item(
                self._settle_running_timeline(timeline, "text", "done"),
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="tool",
                    label=str(payload.get("label") or payload.get("done_label") or payload.get("tool_name") or "Tool"),
                    tool_name=str(payload.get("tool_name") or "") or None,
                    status="running",
                    started_at=existing.started_at if existing else self._now_ms(),
                ),
            )
            next_message.state = "streaming" if next_message.content else "thinking"
        elif event_type == "tool_end":
            item_id = str(payload.get("tool_call_id") or self._last_matching_tool_id(timeline, payload.get("tool_name")) or self._timeline_item_id("tool", payload, f"{payload.get('tool_name') or 'unknown'}_{len(timeline)}"))
            existing = self._find_timeline_item(timeline, item_id)
            is_error = bool(payload.get("is_error"))
            timeline = self._upsert_timeline_item(
                timeline,
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="tool",
                    content=existing.content if existing else None,
                    label=str(payload.get("done_label") or payload.get("label") or payload.get("tool_name") or "Tool"),
                    tool_name=str(payload.get("tool_name") or "") or None,
                    status="error" if is_error else "done",
                    started_at=existing.started_at if existing else self._now_ms(),
                    ended_at=self._now_ms(),
                ),
            )
        elif event_type == "tool_report":
            item_id = str(payload.get("tool_call_id") or self._last_matching_tool_id(timeline, payload.get("tool_name")) or self._timeline_item_id("tool", payload, f"{payload.get('tool_name') or 'unknown'}_{len(timeline)}"))
            existing = self._find_timeline_item(timeline, item_id)
            timeline = self._upsert_timeline_item(
                timeline,
                ChatSessionMessageTimelineItem(
                    id=item_id,
                    kind="tool",
                    content=str(payload.get("summary") or (existing.content if existing else "") or ""),
                    label=(existing.label if existing else None) or str(payload.get("tool_name") or "Tool"),
                    tool_name=str(payload.get("tool_name") or "") or None,
                    status="error" if existing and existing.status == "error" else "done",
                    started_at=existing.started_at if existing else self._now_ms(),
                    ended_at=existing.ended_at if existing else self._now_ms(),
                ),
            )
        elif event_type == "proposal":
            proposal = payload.get("proposal")
            if proposal:
                next_message.proposal = Proposal.model_validate(proposal)
        elif event_type == "response":
            response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
            content = str(response.get("message") or next_message.content)
            thinking = str(response.get("thinking") or next_message.thinking or "")
            next_message.content = content
            next_message.thinking = thinking or None
            proposal = response.get("proposal")
            if proposal:
                next_message.proposal = Proposal.model_validate(proposal)
            metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
            usage = metadata.get("token_usage")
            if isinstance(usage, dict):
                next_message.token_usage = ChatTokenUsage.model_validate(usage)
            timeline = self._finalize_response_timeline(timeline, next_message)
            next_message.state = "done"
        elif event_type == "done":
            next_message = self.settle_stream_message(next_message, "done")
            timeline = list(next_message.timeline or timeline)
        elif event_type == "error":
            next_message = self.settle_stream_message(next_message, "error")
            timeline = list(next_message.timeline or timeline)
            if not next_message.content:
                next_message.content = "请求失败。"
        next_message.timeline = timeline
        return next_message

    def settle_stream_message(self, message: ChatSessionMessage, status: str) -> ChatSessionMessage:
        next_message = message.model_copy(deep=True)
        next_message.state = "error" if status == "error" else "done"
        next_message.timeline = [
            item.model_copy(update={"status": status, "ended_at": item.ended_at or self._now_ms()}) if item.status == "running" else item
            for item in (next_message.timeline or [])
        ]
        return next_message

    def _finalize_response_timeline(
        self,
        timeline: list[ChatSessionMessageTimelineItem],
        message: ChatSessionMessage,
    ) -> list[ChatSessionMessageTimelineItem]:
        next_timeline = self._settle_running_timeline(timeline, "tool", "done")
        next_timeline = self._settle_running_timeline(next_timeline, "thinking", "done")
        next_timeline = self._settle_running_timeline(next_timeline, "text", "done")
        if message.thinking and not any(item.kind == "thinking" and item.content for item in next_timeline):
            next_timeline.insert(
                0,
                ChatSessionMessageTimelineItem(
                    id=f"{message.id}_thinking_final",
                    kind="thinking",
                    content=message.thinking,
                    status="done",
                    started_at=self._now_ms(),
                    ended_at=self._now_ms(),
                ),
            )
        if message.content and not any(item.kind == "text" and item.content for item in next_timeline):
            next_timeline.append(
                ChatSessionMessageTimelineItem(
                    id=f"{message.id}_text_final",
                    kind="text",
                    content=message.content,
                    status="done",
                )
            )
        return next_timeline

    @staticmethod
    def _timeline_item_id(kind: str, payload: dict[str, Any], fallback: str) -> str:
        turn_index = payload.get("assistant_turn_index")
        content_index = payload.get("content_index")
        if content_index is None:
            if fallback.startswith(f"{kind}_"):
                return fallback
            return f"{kind}_{fallback}"
        prefix = f"{turn_index}_" if turn_index is not None else ""
        return f"{kind}_{prefix}{content_index}"

    @staticmethod
    def _find_timeline_item(
        timeline: list[ChatSessionMessageTimelineItem],
        item_id: str,
    ) -> ChatSessionMessageTimelineItem | None:
        return next((item for item in timeline if item.id == item_id), None)

    @staticmethod
    def _upsert_timeline_item(
        timeline: list[ChatSessionMessageTimelineItem],
        item: ChatSessionMessageTimelineItem,
    ) -> list[ChatSessionMessageTimelineItem]:
        for index, existing in enumerate(timeline):
            if existing.id == item.id:
                timeline[index] = item
                return timeline
        timeline.append(item)
        return timeline

    @staticmethod
    def _running_item_id(timeline: list[ChatSessionMessageTimelineItem], kind: str) -> str | None:
        for item in reversed(timeline):
            if item.kind == kind and item.status == "running":
                return item.id
        return None

    @staticmethod
    def _last_text_id(timeline: list[ChatSessionMessageTimelineItem]) -> str | None:
        if timeline and timeline[-1].kind == "text":
            return timeline[-1].id
        return None

    @staticmethod
    def _last_matching_tool_id(timeline: list[ChatSessionMessageTimelineItem], tool_name: object) -> str | None:
        for item in reversed(timeline):
            if item.kind == "tool" and (not tool_name or item.tool_name == tool_name):
                return item.id
        return None

    def _settle_running_timeline(
        self,
        timeline: list[ChatSessionMessageTimelineItem],
        kind: str,
        status: str,
        *,
        exclude_id: str | None = None,
    ) -> list[ChatSessionMessageTimelineItem]:
        return [
            item.model_copy(update={"status": status, "ended_at": item.ended_at or self._now_ms()})
            if item.kind == kind and item.status == "running" and item.id != exclude_id
            else item
            for item in timeline
        ]

    @staticmethod
    def _event_number(payload: dict[str, Any], key: str) -> int | float | None:
        value = payload.get(key)
        if isinstance(value, int | float):
            return value
        return None

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
