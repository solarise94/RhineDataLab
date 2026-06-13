from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.services.card_library_service import CardLibraryService
from app.services.reference_data_service import (
    ReferenceDataError,
    ReferenceDataKind,
    ReferenceDataService,
)

router = APIRouter(prefix="/reference-data", tags=["reference-data"])


def _get_service() -> ReferenceDataService:
    from app.api.deps import get_reference_data_service

    return get_reference_data_service()


def _get_card_library_service() -> CardLibraryService:
    from app.api.deps import get_card_library_service

    return get_card_library_service()


class RegisterFromPathRequest(BaseModel):
    source_path: str
    name: str
    kind: ReferenceDataKind = "other"
    description: str | None = None


@router.get("")
def list_reference_data(service: ReferenceDataService = Depends(_get_service)) -> dict:
    return {"entries": service.list()}


@router.post("")
def register_upload(
    file: UploadFile = File(...),
    name: str = Form(...),
    kind: ReferenceDataKind = Form("other"),
    description: str | None = Form(None),
    service: ReferenceDataService = Depends(_get_service),
) -> dict:
    """Register a reference-data file via multipart upload (always safe).

    Currently only regular files or archives are accepted; upload a directory
    as a zip/tar.gz instead.
    """
    try:
        meta = service.register_upload(
            file.file,
            filename=file.filename or "reference_data",
            name=name,
            kind=kind,
            description=description,
        )
        return {"entry": meta.model_dump()}
    except ReferenceDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/from-path")
def register_from_path(
    body: RegisterFromPathRequest,
    service: ReferenceDataService = Depends(_get_service),
) -> dict:
    """Register a file already on disk. Constrained to approved data/project roots."""
    try:
        meta = service.register_local(
            Path(body.source_path),
            name=body.name,
            kind=body.kind,
            description=body.description,
        )
        return {"entry": meta.model_dump()}
    except ReferenceDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{ref_id}")
def get_reference_data(
    ref_id: str,
    service: ReferenceDataService = Depends(_get_service),
) -> dict:
    try:
        return {"entry": service.get(ref_id)}
    except ReferenceDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{ref_id}")
def delete_reference_data(
    ref_id: str,
    service: ReferenceDataService = Depends(_get_service),
    card_library_service: CardLibraryService = Depends(_get_card_library_service),
) -> dict:
    """Delete a reference-data entry. Blocked if it is still used by blueprints or drafts."""
    usages = card_library_service.reference_usage(ref_id)
    if usages:
        raise HTTPException(
            status_code=409,
            detail={"message": f"Reference {ref_id} is still in use", "references": usages},
        )
    try:
        return service.delete(ref_id)
    except ReferenceDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{ref_id}/download")
def download_reference_data(
    ref_id: str,
    service: ReferenceDataService = Depends(_get_service),
):
    try:
        path, meta = service.download(ref_id)
    except ReferenceDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=meta.original_filename or path.name,
    )
