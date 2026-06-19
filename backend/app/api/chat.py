import logging
from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from starlette.requests import ClientDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import (
    get_chat_job_service,
    get_chat_session_service,
    get_manager_auto_service,
    get_manager_service,
    get_patch_apply_service,
    get_project_service,
    get_runtime_dependency_job_service,
    get_manager_command_service,
)
from app.models.chat import ChatRequest, ChatSessionMessage, ChatSessionMessageTimelineItem
from app.models.graph import Asset
from app.models.patches import GraphPatch
from app.services.chat_job_service import ChatJobService
from app.services.chat_session_service import ChatSessionService
from app.services.manager_auto_service import ManagerAutoService
from app.services.manager_planner import ManagerPlanningError
from app.services.manager_service import ManagerService
from app.services.patch_apply import PatchApplyService
from app.services.project_service import ProjectService
from app.services.provider_errors import ProviderAPIError
from app.services.runtime_dependency_job_service import RuntimeDependencyJobService
from app.services.runtime_dependency_state_service import runtime_dependency_failure_details
from app.services.utils import parse_slash_command, sha256_file, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}", tags=["chat"])

class AcceptProposalRequest(BaseModel):
    session_id: str | None = None


@router.post("/chat", deprecated=True)
def chat(project_id: str, request: ChatRequest, manager_service: ManagerService = Depends(get_manager_service)) -> dict:
    """Legacy synchronous compatibility endpoint. New callers must use /chat-stream."""
    try:
        return manager_service.chat(project_id, request).model_dump()
    except ProviderAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict()) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/chat-stream")
def chat_stream(
    project_id: str,
    request: ChatRequest,
    manager_service: ManagerService = Depends(get_manager_service),
    manager_command_service = Depends(get_manager_command_service),
) -> StreamingResponse:
    is_cmd, cmd_type, obj = parse_slash_command(request.message)
    if is_cmd:
        return StreamingResponse(
            manager_command_service.handle_auto_command_stream(
                project_id, request, cmd_type, obj
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        stream = manager_service.stream_chat(project_id, request)
    except ProviderAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict()) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat-compact")
def chat_compact(project_id: str, request: ChatRequest, manager_service: ManagerService = Depends(get_manager_service)) -> dict:
    try:
        return manager_service.compact_chat_session(project_id, request)
    except ProviderAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict()) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/chat-jobs", deprecated=True)
def create_chat_job(
    project_id: str,
    request: ChatRequest,
    manager_service: ManagerService = Depends(get_manager_service),
    chat_job_service: ChatJobService = Depends(get_chat_job_service),
) -> dict:
    """Legacy background chat endpoint. New callers must use /chat-stream."""
    job = chat_job_service.submit(project_id, request, manager_service.chat)
    return {"job_id": job.job_id, "status": job.status}


@router.get("/chat-jobs/{job_id}", deprecated=True)
def get_chat_job(project_id: str, job_id: str, chat_job_service: ChatJobService = Depends(get_chat_job_service)) -> dict:
    job = chat_job_service.get(job_id)
    if not job or job.project_id != project_id:
        raise HTTPException(status_code=404, detail="Chat job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "response": job.response.model_dump() if job.response else None,
        "error": job.error,
    }


@router.get("/runtime-dependency-jobs/{job_id}")
def get_runtime_dependency_job(
    project_id: str,
    job_id: str,
    runtime_dependency_job_service: RuntimeDependencyJobService = Depends(get_runtime_dependency_job_service),
) -> dict:
    job = runtime_dependency_job_service.get_for_project(project_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Runtime dependency job not found")
    # Return normalized failure details for consistent shape with events and work orders.
    details = runtime_dependency_failure_details(job)
    # P1 observability fields
    details["phase"] = job.phase
    if job.status_detail is not None:
        details["status_detail"] = job.status_detail
    if job.changed is not None:
        details["changed"] = job.changed
    if job.command_preview is not None:
        details["command_preview"] = job.command_preview
    if job.child_pid is not None:
        details["child_pid"] = job.child_pid
    if job.log_path is not None:
        details["log_path"] = job.log_path
    if job.last_heartbeat_at is not None:
        details["last_heartbeat_at"] = job.last_heartbeat_at
    # Layer F2: live progress fields for the determinate progress bar.
    details["progress"] = job.progress
    if job.progress_label is not None:
        details["progress_label"] = job.progress_label
    if job.bytes_total is not None:
        details["bytes_total"] = job.bytes_total
    if job.bytes_downloaded is not None:
        details["bytes_downloaded"] = job.bytes_downloaded
    if job.download_rate_bps is not None:
        details["download_rate_bps"] = job.download_rate_bps
    if job.stdout_tail:
        details["stdout_tail"] = job.stdout_tail
    if job.stderr_tail:
        details["stderr_tail"] = job.stderr_tail
    # Preserve raw payload/result/error for audit compatibility.
    return {
        **details,
        "payload": job.payload,
        "result": job.result,
        "error": job.error,
    }


class MarkRuntimeDependencyResolvedRequest(BaseModel):
    session_id: str
    resolution_message: str = "User confirmed the runtime package was installed manually."


@router.post("/runtime-dependency-jobs/{job_id}/mark-resolved")
def mark_runtime_dependency_job_resolved(
    project_id: str,
    job_id: str,
    body: MarkRuntimeDependencyResolvedRequest,
    runtime_dependency_job_service: RuntimeDependencyJobService = Depends(get_runtime_dependency_job_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
    chat_session_service: ChatSessionService = Depends(get_chat_session_service),
) -> dict:
    # Owner session guard: reject btw session while auto is enabled for another owner.
    auto_state = manager_auto_service.get_state(project_id)
    if auto_state.enabled and auto_state.owner_session_id and auto_state.owner_session_id != body.session_id:
        raise HTTPException(
            status_code=409,
            detail="Another session owns auto mode for this project. Only the owner session may mark dependency blockers resolved.",
        )

    result = runtime_dependency_job_service.mark_job_resolved(
        project_id,
        job_id,
        session_id=body.session_id,
        resolution_message=body.resolution_message,
    )

    # Chat audit: append an operational message in the owner session.
    audit_session_id = auto_state.owner_session_id or body.session_id
    try:
        chat_session_service.append_messages(
            project_id,
            audit_session_id,
            messages=[
                ChatSessionMessage(
                    id=f"dep_resolved_{job_id}_{utc_now()}",
                    role="manager",
                    content=(
                        f"Marked runtime dependency job {job_id} as manually resolved. "
                        f"{body.resolution_message}"
                    ),
                    state="done",
                    timeline=[
                        ChatSessionMessageTimelineItem(
                            id=f"dep_resolved_timeline_{job_id}_{utc_now()}",
                            kind="command",
                            content=f"mark-resolved {job_id}",
                            status="done",
                        ),
                    ],
                ),
            ],
        )
    except Exception:
        # Non-blocking: chat audit failure should not fail the resolution.
        logger.exception("Chat audit failed for mark-resolved: project=%s job=%s", project_id, job_id)

    # Auto wake: if auto is enabled and owner session exists, re-evaluate workboard.
    if auto_state.enabled and auto_state.owner_session_id:
        try:
            state, wake_payload = manager_auto_service.evaluate_workboard_and_maybe_signal(
                project_id,
                auto_state.owner_session_id,
            )
        except Exception:
            # Non-blocking: evaluation failure should not fail the resolution.
            logger.exception("Auto evaluate failed after mark-resolved: project=%s job=%s", project_id, job_id)

    return result


@router.post("/chat-uploads")
async def upload_chat_file(
    project_id: str,
    file: UploadFile = File(...),
    project_service: ProjectService = Depends(get_project_service),
) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    safe_name = _safe_filename(file.filename)
    timestamp = utc_now().replace(":", "").replace("-", "").replace("Z", "")
    asset_id = f"upload_{timestamp}_{uuid4().hex[:8]}_{Path(safe_name).stem[:32].lower()}"
    relative_path = f"data/uploads/{asset_id}_{safe_name}"
    project_root = project_service.project_path(project_id)
    target = project_root / relative_path
    temp_target = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    assets_persisted = False
    asset: Asset | None = None
    digest: str | None = None

    try:
        with temp_target.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                handle.write(chunk)

        digest = sha256_file(temp_target)

        with project_service.lock_for(project_id):
            store = project_service.graph_store(project_id)
            graph = store.load_graph()
            if any(existing.asset_id == asset_id for existing in graph.assets):
                raise HTTPException(status_code=409, detail="Uploaded asset id collision")

            temp_target.replace(target)

            asset = Asset(
                asset_id=asset_id,
                asset_type=_asset_type_for_upload(file.content_type, safe_name),
                title=file.filename,
                status="candidate",
                path=relative_path,
                summary=f"User uploaded file for Manager AI chat. Size: {size} bytes.",
                metadata={
                    "source": "manager_chat_upload",
                    "content_type": file.content_type,
                    "size_bytes": size,
                    "sha256": digest,
                    "uploaded_at": utc_now(),
                },
            )
            graph.assets.append(asset)
            store.save_assets(graph.assets)
            assets_persisted = True

    except ClientDisconnect:
        logger.info("upload aborted by client: project=%s asset=%s", project_id, asset_id)
        raise
    except HTTPException as exc:
        if exc.status_code == 409:
            logger.warning("upload asset id collision: project=%s asset=%s", project_id, asset_id)
        raise
    except Exception:
        if assets_persisted:
            logger.error(
                "upload post-persist failure; file and assets.json may both be committed: %s",
                asset_id,
            )
        logger.exception("upload pipeline failed: project=%s asset=%s", project_id, asset_id)
        raise
    finally:
        await file.close()
        # Only the staging .part is cleaned up here. The final target, once
        # placed, is never removed by the exception path: deleting it after
        # save_assets() has (possibly) committed would force the forbidden
        # state "graph registered, file missing". Orphan final files are
        # handled by reconcile_project_uploads() at backend startup.
        if temp_target.exists():
            temp_target.unlink()

    if asset is None:
        raise HTTPException(status_code=500, detail="Upload failed: asset could not be created")

    return {
        "asset": asset,
        "attachment": {
            "type": "asset",
            "id": asset.asset_id,
            "label": asset.title,
        },
    }


@router.post("/proposals/{proposal_id}/modify")
def modify_proposal(
    project_id: str,
    proposal_id: str,
    request: ChatRequest,
    manager_service: ManagerService = Depends(get_manager_service),
    project_service: ProjectService = Depends(get_project_service),
) -> dict:
    try:
        proposal = manager_service.modify_proposal(project_id, proposal_id, request)
    except ProviderAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.to_dict()) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    patch_payload = project_service.graph_store(project_id).load_patch(proposal.patch_id)
    if not patch_payload:
        raise HTTPException(status_code=404, detail="Modified patch not found")
    return {"proposal": proposal, "patch": patch_payload}


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120] or "upload.bin"


def _asset_type_for_upload(content_type: str | None, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if content_type and content_type.startswith("image/"):
        return "figure"
    if suffix in {".tsv", ".csv", ".xlsx", ".xls"}:
        return "table"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".txt", ".log", ".json", ".yaml", ".yml"}:
        return "text"
    return "uploaded_file"


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(
    project_id: str,
    proposal_id: str,
    request: AcceptProposalRequest | None = Body(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    patch_apply_service: PatchApplyService = Depends(get_patch_apply_service),
    project_service: ProjectService = Depends(get_project_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    try:
        proposal = manager_service.get_proposal(project_id, proposal_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if proposal.status != "proposed":
        raise HTTPException(status_code=400, detail=f"Proposal status is {proposal.status}, cannot accept")
    patch_payload = project_service.graph_store(project_id).load_patch(proposal.patch_id)
    if not patch_payload:
        raise HTTPException(status_code=404, detail="Patch not found")
    try:
        result = patch_apply_service.apply_patch(project_id, GraphPatch.model_validate(patch_payload))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    try:
        proposal = manager_service.mark_proposal_status(project_id, proposal_id, "accepted")
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session_id = request.session_id if request is not None else None
    if session_id:
        auto_state = manager_auto_service.get_state(project_id)
        if auto_state.enabled and auto_state.owner_session_id == session_id:
            manager_auto_service.evaluate_workboard_and_maybe_signal(project_id, session_id)
    return {"proposal": proposal, "apply_result": result, "snapshot": project_service.get_project_snapshot(project_id)}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(project_id: str, proposal_id: str, manager_service: ManagerService = Depends(get_manager_service)) -> dict:
    try:
        proposal = manager_service.reject_proposal(project_id, proposal_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"proposal": proposal}
