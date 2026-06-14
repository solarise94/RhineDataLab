from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse

from app.models.card_blueprint import (
    CardBlueprint,
    CreateProjectDraftRequest,
    InstantiateRequest,
    SaveFromCardRequest,
    UpdateBlueprintRequest,
    UpdateProjectDraftRequest,
)
from app.services.card_library_service import CardLibraryService

router = APIRouter(prefix="/card-library", tags=["card-library"])


def _get_service() -> CardLibraryService:
    from app.api.deps import get_card_library_service
    return get_card_library_service()


@router.get("")
def list_blueprints(service: CardLibraryService = Depends(_get_service)) -> dict:
    return {"entries": service.list_blueprints()}


@router.get("/search")
def search_blueprints(
    query: str = Query(default=""),
    tags: list[str] = Query(default_factory=list),
    domain: str | None = Query(default=None),
    runtime: str | None = Query(default=None),
    top_k: int = Query(default=20, ge=1, le=50),
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    results = service.search_blueprints(
        query=query,
        tags=tags,
        domain=domain,
        runtime=runtime,
        top_k=top_k,
    )
    return {"results": results}


@router.get("/{blueprint_id}")
def get_blueprint(
    blueprint_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        return {"blueprint": service.get_blueprint(blueprint_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("")
def save_from_card(
    body: SaveFromCardRequest,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        result = service.save_from_card(body.project_id, body.card_id)
        return {"blueprint_id": result.blueprint_id, "warnings": result.warnings}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/import")
def import_blueprint(
    body: CardBlueprint,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    result = service.save_from_import(body.model_dump())
    return {"blueprint_id": result.blueprint_id, "warnings": result.warnings}


@router.put("/{blueprint_id}")
def update_blueprint(
    blueprint_id: str,
    body: UpdateBlueprintRequest,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        updated = service.update_blueprint(blueprint_id, body)
        return {"blueprint": updated}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{blueprint_id}")
def delete_blueprint(
    blueprint_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        return service.delete_blueprint(blueprint_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{blueprint_id}/export")
def export_blueprint(
    blueprint_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        return {"blueprint": service.export_blueprint(blueprint_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Cover image
# ---------------------------------------------------------------------------


@router.get("/{blueprint_id}/cover")
def get_cover(
    blueprint_id: str,
    service: CardLibraryService = Depends(_get_service),
):
    try:
        cover_path = service.get_cover_path(blueprint_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if cover_path is None:
        raise HTTPException(status_code=404, detail="No cover image")
    media_types = {
        ".png": "image/png",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
    }
    return FileResponse(
        cover_path,
        media_type=media_types.get(cover_path.suffix.lower(), "application/octet-stream"),
    )


@router.put("/{blueprint_id}/cover")
async def upload_cover(
    blueprint_id: str,
    file: UploadFile = File(...),
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    content = await file.read()
    try:
        result = service.save_cover(blueprint_id, content, file.filename or "cover.png")
        return result
    except ValueError as exc:
        if "too large" in str(exc).lower():
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Project-scoped: instantiate
# ---------------------------------------------------------------------------

project_router = APIRouter(prefix="/projects/{project_id}/card-library", tags=["card-library"])


@project_router.get("")
def list_project_drafts(
    project_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    return {"entries": service.list_project_drafts(project_id)}


@project_router.post("")
def create_project_draft(
    project_id: str,
    body: CreateProjectDraftRequest,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        result = service.create_project_draft(project_id, body.card_id)
        return {"draft_id": result.draft_id, "warnings": result.warnings}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@project_router.get("/{draft_id}")
def get_project_draft(
    project_id: str,
    draft_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        return {"draft": service.get_project_draft(project_id, draft_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@project_router.put("/{draft_id}")
def update_project_draft(
    project_id: str,
    draft_id: str,
    body: UpdateProjectDraftRequest,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        return service.update_project_draft(project_id, draft_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@project_router.delete("/{draft_id}")
def delete_project_draft(
    project_id: str,
    draft_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    return service.delete_project_draft(project_id, draft_id)


@project_router.post("/{draft_id}/review")
def review_project_draft(
    project_id: str,
    draft_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        return service.review_project_draft(project_id, draft_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@project_router.post("/{draft_id}/publish")
def publish_project_draft(
    project_id: str,
    draft_id: str,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    try:
        result = service.publish_project_draft(project_id, draft_id)
        return {"draft_id": result.draft_id, "global_blueprint_id": result.global_blueprint_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@project_router.post("/{blueprint_id}/instantiate")
def instantiate_blueprint(
    project_id: str,
    blueprint_id: str,
    body: InstantiateRequest,
    service: CardLibraryService = Depends(_get_service),
) -> dict:
    result = service.instantiate(blueprint_id, project_id, body)
    return {
        "card_id": result.card_id,
        "warnings": result.warnings,
        "blockers": result.blockers,
    }
