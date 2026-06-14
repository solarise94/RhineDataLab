import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.api.deps import get_flow_service, get_library_registry_service, get_manager_auto_service, get_project_service, get_worker_service
from app.core.config import get_settings
from app.services.flow_service import FlowService
from app.services.library_registry_service import LibraryRegistryService
from app.services.manager_auto_service import ManagerAutoService
from app.services.project_service import ProjectService
from app.services.worker_service import WorkerService

router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    project_id: str
    name: str
    current_goal: str


class UpdateProjectRuntimePreferencesRequest(BaseModel):
    script_preference: Literal["auto", "prefer_python", "prefer_r", "prefer_mixed"] | None = None
    python_runtime: str | None = None
    r_runtime: str | None = None
    execution_mode: Literal["guarded", "workspace_write"] | None = None


class UpdateProjectDataDirectoryRequest(BaseModel):
    root_id: str
    path: str


@router.get("")
def list_projects(project_service: ProjectService = Depends(get_project_service)) -> dict:
    items = project_service.list_projects()
    enriched = []
    for item in items:
        data = item.model_dump()
        if item.data_directory is not None:
            data["data_directory_available"] = Path(item.data_directory.resolved_path).exists()
        else:
            data["data_directory_available"] = None
        enriched.append(data)
    return {"items": enriched}


@router.post("")
def create_project(request: CreateProjectRequest, project_service: ProjectService = Depends(get_project_service)) -> dict:
    project = project_service.create_project(
        project_id=request.project_id,
        name=request.name,
        current_goal=request.current_goal,
    )
    return {"project": project}


@router.delete("/{project_id}")
def delete_project(
    project_id: str,
    delete_directory: bool = False,
    project_service: ProjectService = Depends(get_project_service),
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    if worker_service.has_active_runs(project_id):
        raise HTTPException(status_code=409, detail=f"Project {project_id} has active runs and cannot be deleted.")
    project_service.delete_project(project_id, delete_directory=delete_directory)
    return {"ok": True}


@router.get("/{project_id}")
def get_project(
    project_id: str,
    project_service: ProjectService = Depends(get_project_service),
    manager_auto_service: ManagerAutoService = Depends(get_manager_auto_service),
) -> dict:
    snapshot = project_service.get_project_snapshot_core(project_id)
    snapshot["manager_auto"] = manager_auto_service.get_state(project_id).model_dump()
    mount = project_service.get_project_data_directory(project_id)
    if mount is not None:
        snapshot["data_directory_available"] = Path(mount.resolved_path).exists()
    else:
        snapshot["data_directory_available"] = None
    return snapshot


@router.get("/{project_id}/environment")
def get_project_environment(
    project_id: str,
    project_service: ProjectService = Depends(get_project_service),
) -> dict:
    return project_service.get_project_environment(project_id)


@router.get("/{project_id}/runtime-preferences")
def get_project_runtime_preferences(project_id: str, project_service: ProjectService = Depends(get_project_service)) -> dict:
    return {"runtime_preferences": project_service.get_project_runtime_preferences(project_id).model_dump()}


@router.put("/{project_id}/runtime-preferences")
def update_project_runtime_preferences(
    project_id: str,
    request: UpdateProjectRuntimePreferencesRequest,
    project_service: ProjectService = Depends(get_project_service),
) -> dict:
    return {
        "runtime_preferences": project_service.update_project_runtime_preferences(
            project_id,
            request.model_dump(exclude_unset=True),
        ).model_dump()
    }


@router.put("/{project_id}/data-directory")
def update_project_data_directory(
    project_id: str,
    request: UpdateProjectDataDirectoryRequest,
    project_service: ProjectService = Depends(get_project_service),
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    if worker_service.has_active_runs(project_id):
        raise HTTPException(status_code=409, detail=f"Project {project_id} has active runs and cannot modify data directory.")
    mount = project_service.set_project_data_directory(project_id, request.root_id, request.path)
    return {"data_directory": mount.model_dump()}


@router.get("/{project_id}/data-directory")
def get_project_data_directory(project_id: str, project_service: ProjectService = Depends(get_project_service)) -> dict:
    mount = project_service.get_project_data_directory(project_id)
    available = None
    if mount is not None:
        available = Path(mount.resolved_path).exists()
    return {"data_directory": mount.model_dump() if mount else None, "available": available}


@router.delete("/{project_id}/data-directory")
def detach_project_data_directory(
    project_id: str,
    project_service: ProjectService = Depends(get_project_service),
    worker_service: WorkerService = Depends(get_worker_service),
) -> dict:
    if worker_service.has_active_runs(project_id):
        raise HTTPException(status_code=409, detail=f"Project {project_id} has active runs and cannot detach data directory.")
    mount = project_service.detach_project_data_directory(project_id)
    return {"data_directory": mount.model_dump() if mount else None, "detached": mount is not None}


@router.get("/{project_id}/data-directory/export-history")
def get_project_data_directory_export_history(
    project_id: str,
    project_service: ProjectService = Depends(get_project_service),
) -> dict:
    """Return export history for the project's mounted data directory."""
    if not (project_service.project_path(project_id) / "project.json").exists():
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    store = project_service.graph_store(project_id)
    graph = store.load_graph()
    history = graph.metadata.get("export_history", []) if isinstance(graph.metadata, dict) else []
    history = history or []
    return {"items": history}


@router.get("/{project_id}/skill-library")
def get_skill_library(project_id: str, library_service: LibraryRegistryService = Depends(get_library_registry_service)) -> dict:
    payload = library_service.list_entries("skill")
    payload["project_id"] = project_id
    return payload


@router.get("/{project_id}/mcp-library")
def get_mcp_library(project_id: str, library_service: LibraryRegistryService = Depends(get_library_registry_service)) -> dict:
    payload = library_service.list_entries("mcp")
    payload["project_id"] = project_id
    return payload


class InstallCapabilityRequest(BaseModel):
    kind: str  # "skill" | "mcp"
    source_type: str = "local_path"  # "local_path" | "repo"
    source: str
    overwrite: bool = False


@router.post("/{project_id}/capabilities/install")
def install_capability(
    project_id: str,
    request: InstallCapabilityRequest,
    library_service: LibraryRegistryService = Depends(get_library_registry_service),
) -> dict:
    kind = request.kind.strip()
    if kind not in ("skill", "mcp"):
        raise HTTPException(status_code=422, detail="kind must be 'skill' or 'mcp'")

    if request.source_type not in ("local_path",):
        raise HTTPException(status_code=422, detail="source_type 'repo' is not yet supported; use 'local_path'")

    source = Path(request.source.strip())
    if not source.is_absolute():
        source = Path.cwd() / source
    source = source.resolve()
    if not source.exists():
        raise HTTPException(status_code=422, detail=f"Source path does not exist: {source}")
    if not source.is_dir():
        raise HTTPException(status_code=422, detail="Source must be a directory")

    # Structural validation: verify the source looks like a valid capability
    if kind == "skill" and not (source / "SKILL.md").exists():
        raise HTTPException(status_code=422, detail="Skill source must contain a SKILL.md file")
    if kind == "mcp":
        has_entry = (source / "manifest.json").exists() or (source / "server.json").exists() or (source / "mcp.json").exists()
        if not has_entry:
            raise HTTPException(status_code=422, detail="MCP source must contain at least one of: manifest.json, server.json, mcp.json")

    if kind == "skill":
        try:
            return library_service.install_skill_from_directory(source, overwrite=request.overwrite)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Legacy MCP local-path install
    try:
        return library_service.install_mcp_from_directory(source, overwrite=request.overwrite)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    """Extract zip members while preventing Zip Slip attacks."""
    target_dir = target_dir.resolve()
    for member in archive.infolist():
        if member.is_dir():
            continue
        member_path = Path(member.filename)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"Unsafe archive path: {member.filename}")
        dest = (target_dir / member_path).resolve()
        if not str(dest).startswith(str(target_dir) + "/"):
            raise ValueError(f"Archive path escapes target directory: {member.filename}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as src, dest.open("wb") as dst:
            shutil.copyfileobj(src, dst)


@router.post("/{project_id}/capabilities/skills/upload")
async def upload_skill(
    project_id: str,
    file: UploadFile = File(...),
    overwrite: bool = Form(False),
    library_service: LibraryRegistryService = Depends(get_library_registry_service),
) -> dict:
    if not file.filename:
        raise HTTPException(status_code=422, detail="Filename is required")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".skill", ".zip"}:
        raise HTTPException(status_code=422, detail="File must be a .skill or .zip archive")

    target_id = Path(file.filename).stem
    if not target_id:
        raise HTTPException(status_code=422, detail="Invalid filename")

    try:
        target_id = library_service._validate_capability_id(target_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        archive_path = tmp_path / f"upload{suffix}"
        try:
            with archive_path.open("wb") as dst:
                shutil.copyfileobj(file.file, dst)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read uploaded file: {exc}") from exc

        extracted = tmp_path / "extracted"
        extracted.mkdir()
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                _safe_extract_zip(archive, extracted)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=422, detail="Invalid zip archive") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        members = [p for p in extracted.iterdir() if p.is_dir()]
        if len(members) == 1 and (members[0] / "SKILL.md").exists():
            source_dir = members[0]
        elif (extracted / "SKILL.md").exists():
            # Archive root itself is the skill directory
            source_dir = extracted
        else:
            raise HTTPException(status_code=422, detail="Archive does not contain a valid skill directory")

        try:
            return library_service.install_skill_from_directory(
                source_dir, target_id=target_id, overwrite=overwrite
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


class RegisterMcpServerRequest(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    overwrite: bool = False


@router.post("/{project_id}/capabilities/mcp/register")
def register_mcp_server(
    project_id: str,
    request: RegisterMcpServerRequest,
    library_service: LibraryRegistryService = Depends(get_library_registry_service),
) -> dict:
    try:
        return library_service.register_mcp_server(
            server_id=request.id.strip(),
            name=request.name.strip(),
            transport=request.transport,
            command=request.command,
            args=request.args,
            env=request.env,
            url=request.url,
            headers=request.headers,
            overwrite=request.overwrite,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{project_id}/cards")
def get_cards(project_id: str, project_service: ProjectService = Depends(get_project_service)) -> dict:
    snapshot = project_service.get_project_snapshot(project_id)
    return {"items": snapshot["cards"]}


@router.get("/{project_id}/asset-flow")
def get_asset_flow(project_id: str, flow_service: FlowService = Depends(get_flow_service)) -> dict:
    return flow_service.get_asset_flow(project_id)


@router.get("/{project_id}/work-order")
def get_work_order(project_id: str, flow_service: FlowService = Depends(get_flow_service)) -> dict:
    return flow_service.get_work_order(project_id)
