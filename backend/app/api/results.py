from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.api.deps import get_project_service, get_result_asset_service
from app.models.graph import VALID_INPUT_ASSET_STATUSES
from app.services.project_service import ProjectService
from app.services.result_asset_service import ResultAssetService

router = APIRouter(prefix="/projects/{project_id}", tags=["results"])


@router.get("/results")
def get_results(project_id: str, project_service: ProjectService = Depends(get_project_service)) -> dict:
    snapshot = project_service.get_project_snapshot(project_id)
    assets = snapshot["graph"].assets
    return {
        "accepted": [asset for asset in assets if asset.status == "valid"],
        "candidate": [asset for asset in assets if asset.status == "candidate"],
        "other": [asset for asset in assets if asset.status not in VALID_INPUT_ASSET_STATUSES],
    }


@router.get("/results/{asset_id}")
def get_result_asset_detail(
    project_id: str,
    asset_id: str,
    result_asset_service: ResultAssetService = Depends(get_result_asset_service),
) -> dict:
    return result_asset_service.get_asset_detail(project_id, asset_id)


@router.get("/results/{asset_id}/content")
def get_result_asset_content(
    project_id: str,
    asset_id: str,
    result_asset_service: ResultAssetService = Depends(get_result_asset_service),
) -> FileResponse:
    return result_asset_service.get_asset_content_response(project_id, asset_id)
