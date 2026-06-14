from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from app.api.deps import get_manager_auto_service, get_manager_service, get_project_event_service
from app.core.config import get_settings
from app.services.manager_auto_service import ManagerAutoService
from app.services.manager_blueprint_tools import CardWriteValidationError
from app.services.manager_planner import ManagerPlanningError
from app.services.manager_service import ManagerService
from app.services.project_event_service import ProjectEventService

router = APIRouter(prefix="/internal/manager-tools/projects/{project_id}", tags=["manager-tools"])
logger = logging.getLogger(__name__)

_MUTATING_TOOL_NAMES = {
    "create_card",
    "revise_card_plan",
    "annotate_card",
    "configure_card_execution",
    "delete_card",
    "set_tool_policy",
    "install_runtime_dependencies",
    "promote_workboard_item_to_todo",
    "claim_workboard_item",
    "complete_workboard_item",
    "skip_workboard_item",
    "defer_workboard_item",
    "block_workboard_item_for_user",
    "reopen_workboard_item",
    "submit_claimed_workboard_items",
    "finish_auto_episode",
    "start_card_run",
    "stop_card_run",
    "rerun_card",
    "review_card_run",
    "cleanup_run_history",
    "save_card_template",
    "instantiate_card_template",
    "write_project_memory",
}


def _verify_internal_token(authorization: str | None) -> None:
    settings = get_settings()
    expected = settings.internal_tool_token.get_secret_value() if settings.internal_tool_token else ""
    if not expected:
        raise HTTPException(status_code=503, detail="BLUEPRINT_INTERNAL_TOOL_TOKEN is not configured")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid internal tool token")


def _guard_mutation(
    project_id: str,
    tool_name: str,
    session_id: str | None,
    manager_auto_service: ManagerAutoService,
) -> None:
    if tool_name in _MUTATING_TOOL_NAMES:
        manager_auto_service.assert_mutation_allowed(project_id, session_id, tool_name)


def _mark_auto_running(
    project_id: str,
    session_id: str | None,
    manager_auto_service: ManagerAutoService,
    *,
    active_run_id: str | None = None,
    active_job_id: str | None = None,
    clear_active_run: bool = False,
    clear_active_job: bool = False,
) -> None:
    if not session_id:
        return
    view = manager_auto_service.get_view(project_id, session_id)
    if not view.is_owner:
        return
    manager_auto_service.set_runtime_state(
        project_id,
        state_value="running",
        active_run_id=active_run_id,
        active_job_id=active_job_id,
        clear_active_run=clear_active_run,
        clear_active_job=clear_active_job,
    )


def _emit_tool_project_event(
    project_id: str,
    project_event_service: ProjectEventService,
    *,
    reason: str,
    response: dict,
) -> None:
    card = response.get("card") if isinstance(response, dict) else None
    card_id = card.get("card_id") if isinstance(card, dict) else response.get("card_id") if isinstance(response, dict) else None
    status = card.get("status") if isinstance(card, dict) else response.get("status") if isinstance(response, dict) else None
    if card_id is None and reason in {"card_created", "card_updated", "card_annotated", "card_deleted", "card_execution_configured", "card_template_instantiated"}:
        logger.warning("Project event for %s has no card_id: project_id=%s response_keys=%s", reason, project_id, list(response.keys()) if isinstance(response, dict) else None)
    try:
        project_event_service.emit(project_id, reason=reason, card_id=card_id, status=status)
    except Exception:
        logger.exception("Failed to emit manager tool project event: project_id=%s reason=%s card_id=%s", project_id, reason, card_id)


# Run-control project-state events are emitted by WorkerService, which owns run
# lifecycle persistence. This helper is only for direct card/template mutations
# performed by manager tools.


@router.get("/context")
def get_project_context(
    project_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    return manager_service.blueprint_tools.get_project_context(project_id)


@router.get("/inspect")
def inspect_project_summary(
    project_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.inspect_project_summary(project_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/background-workboard")
def get_background_workboard(
    project_id: str,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_background_workboard(project_id, x_blueprint_session_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/dependency-attention/inspect")
def inspect_dependency_attention(
    project_id: str,
    payload: dict | None = None,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.inspect_dependency_attention(project_id, payload or {})
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/data-assets")
def list_data_assets(
    project_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.list_data_assets(project_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cards/find")
def find_cards(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.find_cards(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/assets/find")
def find_assets(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.find_assets(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/data-directory/list")
def list_data_directory(
    project_id: str,
    payload: dict | None = None,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.list_data_directory(project_id, payload or {})
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/data-directory/register")
def register_data_asset(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.register_data_asset(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/data-assets/{asset_id}/describe")
def describe_data_asset(
    project_id: str,
    asset_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.describe_data_asset(project_id, asset_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/data-assets/export")
def export_result(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.export_result(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cards")
def create_card(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
    project_event_service: ProjectEventService = Depends(get_project_event_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "create_card", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.create_card(project_id, payload)
        _emit_tool_project_event(project_id, project_event_service, reason="card_created", response=response)
        return response
    except CardWriteValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.payload) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/cards/{card_id}/detail")
def get_card_detail(
    project_id: str,
    card_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_card_detail(project_id, card_id)
    except CardWriteValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.payload) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/cards/{card_id}")
def revise_card_plan(
    project_id: str,
    card_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
    project_event_service: ProjectEventService = Depends(get_project_event_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "revise_card_plan", x_blueprint_session_id, manager_auto_service)
    try:
        body = dict(payload)
        body["card_id"] = card_id
        response = manager_service.blueprint_tools.update_card(project_id, body)
        _emit_tool_project_event(project_id, project_event_service, reason="card_updated", response=response)
        return response
    except CardWriteValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.payload) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cards/{card_id}/annotate")
def annotate_card(
    project_id: str,
    card_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
    project_event_service: ProjectEventService = Depends(get_project_event_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "annotate_card", x_blueprint_session_id, manager_auto_service)
    try:
        body = dict(payload)
        body["card_id"] = card_id
        response = manager_service.blueprint_tools.annotate_card(project_id, body)
        _emit_tool_project_event(project_id, project_event_service, reason="card_annotated", response=response)
        return response
    except CardWriteValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.payload) from exc
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/card-execution")
def configure_card_execution(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
    project_event_service: ProjectEventService = Depends(get_project_event_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "configure_card_execution", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.configure_card_execution(project_id, payload)
        _emit_tool_project_event(project_id, project_event_service, reason="card_execution_configured", response=response)
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/cards/{card_id}")
def delete_card(
    project_id: str,
    card_id: str,
    payload: dict | None = None,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
    project_event_service: ProjectEventService = Depends(get_project_event_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "delete_card", x_blueprint_session_id, manager_auto_service)
    try:
        body = dict(payload or {})
        body["card_id"] = card_id
        response = manager_service.blueprint_tools.delete_card(project_id, body)
        _emit_tool_project_event(project_id, project_event_service, reason="card_deleted", response=response)
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tool-policy")
def get_tool_policy(
    project_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_tool_policy(project_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/tool-policy")
def set_tool_policy(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "set_tool_policy", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.set_tool_policy(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/assets/{asset_id}/detail")
def get_asset_detail(
    project_id: str,
    asset_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_asset_detail(project_id, asset_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/assets/{asset_id}")
def read_result_asset(
    project_id: str,
    asset_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.read_result_asset(project_id, asset_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/skill-library")
def list_skill_library(
    project_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.list_skill_library(project_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/skill-library/search")
def search_skill_library(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.search_skill_library(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/skill-library/{skill_id}")
def get_skill_library_item(
    project_id: str,
    skill_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_skill_library_item(project_id, skill_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/mcp-library")
def list_mcp_library(
    project_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.list_mcp_library(project_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/mcp-library/search")
def search_mcp_library(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.search_mcp_library(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/mcp-library/{entry_id}")
def get_mcp_library_item(
    project_id: str,
    entry_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_mcp_library_item(project_id, entry_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ------------------------------------------------------------------
# Portable Card Package endpoints
# ------------------------------------------------------------------

@router.post("/packages/search")
def search_card_packages(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.search_card_packages(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/packages/{package_id}")
def get_card_package_detail(
    project_id: str,
    package_id: str,
    version: str | None = None,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_card_package_detail(project_id, package_id, version)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/packages/import")
def import_card_package(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "import_card_package", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.import_card_package(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/packages/{package_id}/instantiate")
def instantiate_card_package(
    project_id: str,
    package_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "instantiate_card_package", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.instantiate_card_package(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runtime-dependencies/install")
def install_runtime_dependencies(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "install_runtime_dependencies", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.install_runtime_dependencies(project_id, payload, x_blueprint_session_id)
        if response.get("job_id"):
            _mark_auto_running(
                project_id,
                x_blueprint_session_id,
                manager_auto_service,
                active_job_id=str(response.get("job_id")),
            )
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runtime-dependencies/resolve")
def resolve_runtime_dependencies(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    # resolve_runtime_dependencies is intentionally available in btw mode
    # because it never mutates runtime state.  We do NOT call
    # _guard_mutation here, and we deliberately omit this tool name from
    # _MUTATING_TOOL_NAMES so that later additions to that set cannot
    # inadvertently block it.
    try:
        return manager_service.blueprint_tools.resolve_runtime_dependencies(
            project_id, payload, x_blueprint_session_id
        )
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runtime-dependencies/jobs/{job_id}")
def get_runtime_dependency_install_status(
    project_id: str,
    job_id: str,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.get_runtime_dependency_install_status(project_id, job_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/promote")
def promote_workboard_item_to_todo(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "promote_workboard_item_to_todo", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.promote_workboard_item_to_todo(project_id, payload, x_blueprint_session_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/claim")
def claim_workboard_item(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "claim_workboard_item", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.claim_workboard_item(project_id, payload, x_blueprint_session_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/skip")
def skip_workboard_item(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "skip_workboard_item", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.skip_workboard_item(project_id, payload, x_blueprint_session_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/complete")
def complete_workboard_item(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "complete_workboard_item", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.complete_workboard_item(project_id, payload, x_blueprint_session_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/defer")
def defer_workboard_item(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "defer_workboard_item", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.defer_workboard_item(project_id, payload, x_blueprint_session_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/block")
def block_workboard_item_for_user(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "block_workboard_item_for_user", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.block_workboard_item_for_user(project_id, payload, x_blueprint_session_id)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/reopen")
def reopen_workboard_item(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "reopen_workboard_item", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.reopen_workboard_item(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/background-workboard/submit-claimed")
def submit_claimed_workboard_items(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "submit_claimed_workboard_items", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.submit_claimed_workboard_items(project_id, payload, x_blueprint_session_id)
        if response.get("run_id"):
            _mark_auto_running(
                project_id,
                x_blueprint_session_id,
                manager_auto_service,
                active_run_id=str(response.get("run_id")),
            )
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/finish-auto-episode")
def finish_auto_episode(
    project_id: str,
    payload: dict = {},
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    """Doc 42 Section 6: Manager tool to finish the current auto episode."""
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "finish_auto_episode", x_blueprint_session_id, manager_auto_service)
    return manager_auto_service.finish_auto_episode(
        project_id,
        x_blueprint_session_id or "",
        complete_message=str(payload.get("complete_message", "") or ""),
    )


@router.post("/runs/start")
def start_card_run(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "start_card_run", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.start_card_run(project_id, payload)
        if response.get("ok") and response.get("can_start", True) and response.get("run_id"):
            _mark_auto_running(
                project_id,
                x_blueprint_session_id,
                manager_auto_service,
                active_run_id=str(response.get("run_id")),
            )
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runs/stop")
def stop_card_run(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "stop_card_run", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.stop_card_run(project_id, payload)
        if response.get("stopped"):
            _mark_auto_running(
                project_id,
                x_blueprint_session_id,
                manager_auto_service,
                clear_active_run=True,
            )
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runs/rerun")
def rerun_card(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "rerun_card", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.rerun_card(project_id, payload)
        if response.get("ok") and response.get("run_id"):
            _mark_auto_running(
                project_id,
                x_blueprint_session_id,
                manager_auto_service,
                active_run_id=str(response.get("run_id")),
            )
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runs/review")
def review_card_run(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "review_card_run", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.review_card_run(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runs/cleanup-history")
def cleanup_run_history(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "cleanup_run_history", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.cleanup_run_history(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/card-templates/search")
def search_card_templates(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.search_card_templates(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/card-templates")
def save_card_template(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "save_card_template", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.save_card_template(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/card-templates/instantiate")
def instantiate_card_template(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
    project_event_service: ProjectEventService = Depends(get_project_event_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "instantiate_card_template", x_blueprint_session_id, manager_auto_service)
    try:
        response = manager_service.blueprint_tools.instantiate_card_template(project_id, payload)
        _emit_tool_project_event(project_id, project_event_service, reason="card_template_instantiated", response=response)
        return response
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/memory/list")
def list_project_memory(
    project_id: str,
    payload: dict | None = None,
    authorization: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
) -> dict:
    _verify_internal_token(authorization)
    try:
        return manager_service.blueprint_tools.list_project_memory(project_id, payload or {})
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/memory")
def write_project_memory(
    project_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_blueprint_session_id: str | None = Header(default=None),
    manager_service: ManagerService = Depends(get_manager_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    _verify_internal_token(authorization)
    _guard_mutation(project_id, "write_project_memory", x_blueprint_session_id, manager_auto_service)
    try:
        return manager_service.blueprint_tools.write_project_memory(project_id, payload)
    except ManagerPlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
