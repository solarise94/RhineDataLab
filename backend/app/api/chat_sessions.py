import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_chat_session_service
from app.models.chat import ChatSessionMessage
from app.services.chat_session_service import ChatSessionService

router = APIRouter(prefix="/projects/{project_id}/chat-sessions", tags=["chat-sessions"])


class CreateChatSessionRequest(BaseModel):
    summary: str | None = None


class SaveChatSessionRequest(BaseModel):
    messages: list[ChatSessionMessage] = Field(default_factory=list)
    summary: str | None = None
    base_revision: int | None = None


class AppendChatSessionMessagesRequest(BaseModel):
    messages: list[ChatSessionMessage] = Field(default_factory=list)
    dedupe_ids: list[str] = Field(default_factory=list)


@router.get("")
def list_chat_sessions(project_id: str, chat_session_service: ChatSessionService = Depends(get_chat_session_service)) -> dict:
    return {"items": chat_session_service.list_sessions(project_id)}


@router.post("")
def create_chat_session(
    project_id: str,
    request: CreateChatSessionRequest,
    chat_session_service: ChatSessionService = Depends(get_chat_session_service),
) -> dict:
    return {"session": chat_session_service.create_session(project_id, request.summary)}


@router.get("/{session_id}")
def get_chat_session(
    project_id: str,
    session_id: str,
    chat_session_service: ChatSessionService = Depends(get_chat_session_service),
) -> dict:
    try:
        return {"session": chat_session_service.get_session(project_id, session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{session_id}/events")
def stream_chat_session_events(
    project_id: str,
    session_id: str,
    chat_session_service: ChatSessionService = Depends(get_chat_session_service),
) -> StreamingResponse:
    try:
        chat_session_service.get_session(project_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    def iterator():
        for event in chat_session_service.subscribe_events(project_id, session_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.put("/{session_id}")
def save_chat_session(
    project_id: str,
    session_id: str,
    request: SaveChatSessionRequest,
    chat_session_service: ChatSessionService = Depends(get_chat_session_service),
) -> dict:
    """Legacy full-session save. Deprecated: /chat-stream now persists messages
    server-side when BLUEPRINT_CHAT_SINGLE_SOURCE is enabled."""
    try:
        return {
            "session": chat_session_service.save_session(
                project_id,
                session_id,
                request.messages,
                request.summary,
                base_revision=request.base_revision,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{session_id}/messages")
def append_chat_session_messages(
    project_id: str,
    session_id: str,
    request: AppendChatSessionMessagesRequest,
    chat_session_service: ChatSessionService = Depends(get_chat_session_service),
) -> dict:
    try:
        return {"session": chat_session_service.append_messages(project_id, session_id, request.messages, request.dedupe_ids)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{session_id}")
def delete_chat_session(
    project_id: str,
    session_id: str,
    chat_session_service: ChatSessionService = Depends(get_chat_session_service),
) -> dict:
    try:
        chat_session_service.delete_session(project_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}
