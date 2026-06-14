import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.api.deps import (
    get_manifest_service,
    get_project_service,
    get_runtime_approval_service,
    get_worker_service,
)
from app.services.manifest_service import ManifestService
from app.services.project_service import ProjectService
from app.services.runtime_approval_service import RuntimeApprovalService
from app.services.worker_service import WorkerService

router = APIRouter(prefix="/projects/{project_id}", tags=["runs"])


class ReviewRunRequest(BaseModel):
    accept: bool = True


class StartRunRequest(BaseModel):
    worker_type: str | None = None
    profile_id: str | None = None
    python_runtime: str | None = None
    r_runtime: str | None = None


class RuntimeApprovalDecisionRequest(BaseModel):
    approve: bool


class CancelRunRequest(BaseModel):
    reason: str | None = None


class CleanupRunRequest(BaseModel):
    reason: str | None = None


class RerunCardRequest(BaseModel):
    worker_type: str | None = None
    profile_id: str | None = None
    python_runtime: str | None = None
    r_runtime: str | None = None


@router.post("/cards/{card_id}/start-run")
def start_run(
    project_id: str,
    card_id: str,
    request: StartRunRequest | None = None,
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    return worker_service.start_run(
        project_id,
        card_id,
        worker_type=request.worker_type if request else None,
        profile_id=request.profile_id if request else None,
        python_runtime=request.python_runtime if request else None,
        r_runtime=request.r_runtime if request else None,
    )


@router.post("/cards/{card_id}/reset-run-state")
def reset_card_run_state(
    project_id: str,
    card_id: str,
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    return worker_service.reset_card_run_state(project_id, card_id)


@router.post("/cards/{card_id}/rerun")
def rerun_card(
    project_id: str,
    card_id: str,
    request: RerunCardRequest | None = None,
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    return worker_service.rerun_card(
        project_id,
        card_id,
        worker_type=request.worker_type if request else None,
        profile_id=request.profile_id if request else None,
        python_runtime=request.python_runtime if request else None,
        r_runtime=request.r_runtime if request else None,
    )


@router.get("/runs/{run_id}")
def get_run(project_id: str, run_id: str, project_service: ProjectService = Depends(get_project_service)) -> dict:
    graph = project_service.graph_store(project_id).load_graph()
    run = next((item for item in graph.runs if item.run_id == run_id), None)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return {"run": run}


@router.get("/runs/{run_id}/events")
def get_run_events(project_id: str, run_id: str, project_service: ProjectService = Depends(get_project_service)) -> dict:
    return {"items": project_service.graph_store(project_id).load_run_events(run_id)}


@router.get("/runs/{run_id}/manifest")
def get_run_manifest(project_id: str, run_id: str, manifest_service: ManifestService = Depends(get_manifest_service)) -> dict:
    ok, errors = manifest_service.validate_manifest(project_id, run_id)
    return {
        "manifest": manifest_service.load_manifest(project_id, run_id),
        "valid": ok,
        "errors": errors,
        "review_context": manifest_service.manifest_to_review_context(project_id, run_id) if ok else None,
    }


@router.post("/runs/{run_id}/review")
def review_run(
    project_id: str,
    run_id: str,
    request: ReviewRunRequest,
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    return worker_service.review_run(project_id, run_id, accept=request.accept)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    project_id: str,
    run_id: str,
    request: CancelRunRequest | None = None,
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    return worker_service.cancel_run(project_id, run_id, reason=request.reason if request else None)


@router.post("/runs/{run_id}/cleanup")
def cleanup_run(
    project_id: str,
    run_id: str,
    request: CleanupRunRequest | None = None,
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    return worker_service.cleanup_run(project_id, run_id, reason=request.reason if request else None)


@router.get("/runs/{run_id}/runtime-approvals")
def get_runtime_approvals(
    project_id: str,
    run_id: str,
    runtime_approval_service: RuntimeApprovalService = Depends(get_runtime_approval_service),
) -> dict:
    return {"items": runtime_approval_service.load_decisions(project_id, run_id)}


@router.post("/runs/{run_id}/runtime-approvals/{request_id}")
def decide_runtime_approval(
    project_id: str,
    run_id: str,
    request_id: str,
    request: RuntimeApprovalDecisionRequest,
    runtime_approval_service: RuntimeApprovalService = Depends(get_runtime_approval_service),
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    decision = runtime_approval_service.decide_request(project_id, run_id, request_id, approve=request.approve)
    if request.approve and not runtime_approval_service.unresolved_user_requests(project_id, run_id):
        worker_service.continue_run_after_approval(project_id, run_id)
    return {"decision": decision}


@router.websocket("/runs/{run_id}/ws")
async def run_events_ws(project_id: str, run_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    store = get_project_service().graph_store(project_id)
    sent = 0
    disconnected = False

    async def _watch_client() -> None:
        """Drain incoming messages; exits when the client disconnects."""
        nonlocal disconnected
        try:
            while True:
                await websocket.receive()
        except Exception:
            disconnected = True

    watcher = asyncio.ensure_future(_watch_client())
    try:
        while not disconnected:
            events = store.load_run_events(run_id)
            while sent < len(events):
                await websocket.send_json(events[sent].model_dump())
                sent += 1
            graph = store.load_graph()
            run = next((item for item in graph.runs if item.run_id == run_id), None)
            if run and run.status in {"success", "failed", "cancelled", "reviewed"} and sent >= len(events):
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        disconnected = True
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        if not disconnected:
            try:
                await websocket.close()
            except Exception:
                pass
