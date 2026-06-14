# Modification Plan: Runtime Sync + Package Capability

> Based on implementation review findings. 2026-06-12.

---

## P0-1: Runtime Sync — Card Dropdown Persistence

### Problem

Sidebar writes `/runtime-preferences` (project-level), but card dropdown only writes Zustand local state. On `startRun`, runtime values come from local store, not from persisted card executor context. This means:
- Card dropdown selections are lost on page refresh.
- Sidebar changes don't flow into the card's `executor_context.runtime_bindings` — the only persisted path is `configure_card_execution`, which Manager uses but the UI never calls.

### Files to Change

#### 1. `frontend/lib/api.ts` — add `configureCardExecution` API client

Add after line ~296 (after `updateProjectRuntimePreferences`):

```ts
configureCardExecution(projectId: string, payload: {
  card_id?: string;
  card_ids?: string[];
  runtime_bindings?: { conda_env?: string | null; r_env?: string | null };
  skills?: string[];
  mcp_servers?: string[];
  instruction_blocks?: string[];
}) {
  return request<{ cards: Card[]; updated_card_ids: string[] }>(
    `/internal/manager-tools/projects/${projectId}/card-execution`,
    { method: "POST", body: JSON.stringify(payload) }
  );
},
```

#### 2. `frontend/lib/hooks.ts` — add mutation hook

Wrap `api.configureCardExecution` in a `useMutation` following the pattern used by `useUpdateProjectRuntimePreferencesMutation`.

#### 3. `frontend/components/layout/ProjectWorkspace.tsx` — persist card runtime on dropdown change

**Lines 738–747** — `onSelectPythonRuntime` / `onSelectRRuntime` currently only set Zustand:

```tsx
// BEFORE (line 738-741)
onSelectPythonRuntime={(card, runtime) => {
  if (autoLocked) return;
  setSelectedPythonRuntime(projectId, card.card_id, runtime);
  setNotice(...);
}}
```

**AFTER**: call `configureCardExecution` API to persist into `card.executor_context.runtime_bindings`:

```tsx
onSelectPythonRuntime={(card, runtime) => {
  if (autoLocked) return;
  setSelectedPythonRuntime(projectId, card.card_id, runtime);
  configureCardExecutionMutation.mutateAsync({
    card_id: card.card_id,
    runtime_bindings: {
      conda_env: runtime === "__system__" ? null : (runtime || null),
    },
  }).catch((error) => reportActionError(error, "保存 card Python runtime 失败。"));
  setNotice(...);
}}
```

Same pattern for `onSelectRRuntime`. The Zustand store update stays (for immediate UI reactivity), but now also persists via API.

#### 4. `frontend/components/layout/ProjectWorkspace.tsx` — read card override from persisted data

**Lines 622–623** — `startRun` currently reads from local store only:

```ts
// BEFORE
const pythonRuntime = selectedPythonRuntimeByProject[card.card_id] ?? effectiveGlobalPythonRuntime ?? "__system__";
const rRuntime = selectedRRuntimeByProject[card.card_id] ?? effectiveGlobalRRuntime ?? "__system__";
```

**AFTER**: read from `card.executor_context.runtime_bindings` first (persisted), fall back to global:

```ts
const cardCtx = card.executor_context;
const pythonRuntime = cardCtx?.runtime_bindings?.conda_env
  ?? effectiveGlobalPythonRuntime
  ?? "__system__";
const rRuntime = cardCtx?.runtime_bindings?.r_env
  ?? effectiveGlobalRRuntime
  ?? "__system__";
```

#### 5. `frontend/components/layout/ProjectWorkspace.tsx` — init Zustand from persisted card data

**Lines 488–491** — currently init Zustand globals from `projectRuntimePreferences`. Also need to init per-card Zustand state from `card.executor_context.runtime_bindings` for each card, so the dropdown shows the persisted value on page load:

```ts
// After global init (line ~491), for each card:
for (const card of snapshot.cards) {
  const bindings = card.executor_context?.runtime_bindings;
  if (bindings?.conda_env !== undefined) {
    setSelectedPythonRuntime(projectId, card.card_id, bindings.conda_env ?? undefined);
  }
  if (bindings?.r_env !== undefined) {
    setSelectedRRuntime(projectId, card.card_id, bindings.r_env ?? undefined);
  }
}
```

#### 6. `backend/app/services/manager_blueprint_tools.py` — `configure_card_execution` also write `runtime_source`

**Lines 919–922** — currently sets `conda_env` / `r_env` but doesn't set `runtime_source`. Add:

```python
if "conda_env" in runtime_bindings:
    context.runtime_bindings.conda_env = runtime_bindings.get("conda_env")
    context.runtime_bindings.runtime_source = "card_override"  # ADD
if "r_env" in runtime_bindings:
    context.runtime_bindings.r_env = runtime_bindings.get("r_env")
    context.runtime_bindings.runtime_source = "card_override"  # ADD
```

#### 7. Clear override semantics

When a user selects `__global__` (follow project default) in the card dropdown, send `null` for that runtime field. The backend `configure_card_execution` should clear the card override for that runtime, falling back to project default at run time.

---

## P0-2: Manager Package Tool Registration

### Problem

`manager-agent/src/server.js` has no tool definitions for `search_card_packages`, `get_card_package_detail`, `import_card_package`, `instantiate_card_package`. The tool list ends after `get_mcp_library_item` at line 2787. Manager cannot discover/import/instantiate packages.

Backend endpoints and `ManagerBlueprintTools` methods already exist.

### Files to Change

#### 1. `manager-agent/src/server.js` — add 4 tool definitions

Insert after line 2787 (`get_mcp_library_item` closing `},`) and before line 2788 (`];`):

```js
    {
      name: "search_card_packages",
      label: "Search card packages",
      description:
        "Search the portable card package registry. Returns id/name/summary/tags/compatibility matches. " +
        "Use this first to discover packages before calling get_card_package_detail.",
      parameters: Type.Object({
        query: Type.Optional(Type.String()),
        runtime: Type.Optional(Type.String()),
        tags: Type.Optional(Type.Array(Type.String())),
        top_k: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "search_card_packages",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/card-packages/search`,
          { q: params.query, runtime: params.runtime, tags: params.tags, top_k: params.top_k },
          signal,
          sessionId,
        );
        return toolTextResult("search_card_packages", payload);
      },
    },
    {
      name: "get_card_package_detail",
      label: "Get card package detail",
      description:
        "Read one portable card package with full manifest and bundle file listing. " +
        "Use this after search_card_packages when a package id is ambiguous or you need to confirm compatibility before import.",
      parameters: Type.Object({
        package_id: Type.String(),
        version: Type.Optional(Type.String()),
      }),
      execute: async (toolCallId, params, signal) => {
        let path = `/internal/manager-tools/projects/${projectId}/card-packages/${encodeURIComponent(params.package_id)}`;
        if (params.version) path += `?version=${encodeURIComponent(params.version)}`;
        const payload = await callLoggedTool(
          "get_card_package_detail",
          toolCallId,
          projectId,
          baseUrl,
          token,
          path,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_card_package_detail", payload);
      },
    },
    {
      name: "import_card_package",
      label: "Import card package",
      description:
        "Import a portable card package from a local directory or zip archive path on the server. " +
        "This is a mutation — it stores the package in the local registry. " +
        "Call this only after the package has been reviewed (via search/detail).",
      parameters: Type.Object({
        source_path: Type.String(),
        overwrite: Type.Optional(Type.Boolean()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "import_card_package",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/card-packages/import`,
          { source_path: params.source_path, overwrite: params.overwrite },
          signal,
          sessionId,
        );
        return toolTextResult("import_card_package", payload);
      },
    },
    {
      name: "instantiate_card_package",
      label: "Instantiate card package",
      description:
        "Create a card from an imported package. Requires package_id. " +
        "Optionally bind input assets, set parameter overrides, or pin a runtime override. " +
        "This is a mutation — it creates a new card in the project. " +
        "Always search/detail first, then instantiate with explicit bindings.",
      parameters: Type.Object({
        package_id: Type.String(),
        version: Type.Optional(Type.String()),
        input_bindings: Type.Optional(Type.Record(Type.String(), Type.String())),
        parameter_bindings: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
        runtime_override: Type.Optional(Type.Record(Type.String(), Type.String())),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "instantiate_card_package",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/card-packages/instantiate`,
          {
            package_id: params.package_id,
            version: params.version,
            input_bindings: params.input_bindings,
            parameter_bindings: params.parameter_bindings,
            runtime_override: params.runtime_override,
          },
          signal,
          sessionId,
        );
        return toolTextResult("instantiate_card_package", payload);
      },
    },
```

#### 2. `manager-agent/src/server.js` — add to `mutatingTools`

In the `mutatingTools` set (line ~2789), add:

```js
"import_card_package",
"instantiate_card_package",
```

#### 3. Backend API routes for package tools

Verify the backend has routes wired for these endpoints (likely in `backend/app/api/manager_tools.py`). If not present, add:

```python
# POST /internal/manager-tools/projects/{project_id}/card-packages/search
# GET  /internal/manager-tools/projects/{project_id}/card-packages/{package_id}
# POST /internal/manager-tools/projects/{project_id}/card-packages/import
# POST /internal/manager-tools/projects/{project_id}/card-packages/instantiate
```

---

## P1-1: Runtime Dependency Resolution Integration

### Problem

`PackageService.instantiate_package` does `_resolve_runtime_for_package` which only does name-level comparison (manifest requirement vs project default). It does not consult `RuntimeDependencyResolverService` to check whether a runtime actually satisfies dependencies, nor does it check `RuntimeDependencyStateService` for in-progress/failed install jobs.

### Files to Change

#### 1. `backend/app/services/package_service.py` — inject resolver services

**Line 74–83** — update `__init__`:

```python
def __init__(
    self,
    library_registry_service: LibraryRegistryService,
    project_service: ProjectService,
    runtime_dependency_resolver: RuntimeDependencyResolverService | None = None,  # ADD
    runtime_dependency_state: RuntimeDependencyStateService | None = None,        # ADD
    settings: Settings | None = None,
) -> None:
    self.library_registry_service = library_registry_service
    self.project_service = project_service
    self.runtime_dependency_resolver = runtime_dependency_resolver    # ADD
    self.runtime_dependency_state = runtime_dependency_state          # ADD
    self.settings = settings or get_settings()
    self.packages_root = Path(self.settings.data_root) / "_system" / "packages"
```

#### 2. `backend/app/api/deps.py` — wire resolver into PackageService construction

**Line 159–164** — update `get_package_service`:

```python
@lru_cache
def get_package_service() -> PackageService:
    return PackageService(
        get_library_registry_service(),
        get_project_service(),
        get_runtime_dependency_resolver_service(),   # ADD
        get_runtime_dependency_state_service(),       # ADD
    )
```

#### 3. `backend/app/services/package_service.py` — add `_resolve_dependencies` method

New method in `PackageService`, called inside `instantiate_package` before creating the card:

```python
def _resolve_dependencies(
    self,
    manifest: PackageManifest,
    project_id: str,
    eff_python: str | None,
    eff_r: str | None,
) -> dict:
    """Check whether resolved runtimes satisfy package dependencies.
    
    Returns {"ok": True} or {"ok": False, "blockers": [...], "warnings": [...]}.
    """
    if self.runtime_dependency_resolver is None:
        return {"ok": True}  # no resolver available → skip check

    blockers = []
    warnings = []

    # Build resolver requests from manifest dependency declarations
    # (If manifest doesn't yet have a dependencies field, this is a no-op)
    deps = getattr(manifest, "dependencies", None) or []
    for dep in deps:
        result = self.runtime_dependency_resolver.resolve(
            project_id=project_id,
            family=dep.family,       # "python" | "r"
            packages=dep.packages,
            target_runtime=eff_python if dep.family == "python" else eff_r,
        )
        if result.status == "blocked":
            blockers.append(f"{dep.family}: {result.message}")
        elif result.status == "install_in_progress":
            warnings.append(f"{dep.family}: install in progress for {dep.packages}")
        elif result.status == "missing_but_installable":
            warnings.append(f"{dep.family}: {dep.packages} not installed; Manager should install first")

    return {
        "ok": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
    }
```

#### 4. `backend/app/services/package_service.py` — call `_resolve_dependencies` in `instantiate_package`

After `_resolve_runtime_for_package` returns and before card creation, call `_resolve_dependencies`. If blockers exist, return `PackageInstantiationResult` with blockers (card not created). If only warnings, create the card but include warnings.

---

## P1-2: Runtime Source Model — Split Python/R

### Problem

`RuntimeBindings.runtime_source` is a single field (`backend/app/models/executor.py:28`), but Python and R runtimes can come from different sources (e.g., Python follows project default, R comes from package requirement). Currently at `package_service.py:253`, it picks `python_source if eff_python else r_source`, which is lossy.

### Files to Change

#### 1. `backend/app/models/executor.py` — add split fields

**Line 28** — keep `runtime_source` for backward compat, add new fields:

```python
class RuntimeBindings(BaseModel):
    conda_env: str | None = None
    r_env: str | None = None
    container_image: str | None = None
    working_dir: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    runtime_source: str | None = None
    """DEPRECATED: single-field source. Use python_runtime_source / r_runtime_source instead."""
    python_runtime_source: str | None = None
    """Source of conda_env: project_default | package_requirement | card_override | __system__"""
    r_runtime_source: str | None = None
    """Source of r_env: project_default | package_requirement | card_override | __system__"""
```

#### 2. `backend/app/services/package_service.py` — populate both source fields

**Line 250–254** — update `instantiate_package`:

```python
runtime_bindings = RuntimeBindings(
    conda_env=eff_python,
    r_env=eff_r,
    runtime_source=python_source if eff_python else r_source,  # keep for compat
    python_runtime_source=python_source,  # ADD
    r_runtime_source=r_source,            # ADD
)
```

#### 3. `backend/app/services/package_service.py` — `PackageInstantiationResult` return both

Update the return dict in `instantiate_card_package` (manager_blueprint_tools.py line ~1165) to include `python_runtime_source` and `r_runtime_source`.

---

## P1-3: Package Archive & Security

### Problem A: Zip import only reads `manifest.json`, not `bundle/` files

`_load_manifest_dict` supports zip (line 397–404), but `_load_bundle_files` returns empty for non-directory sources (line 409–410). This means zip-imported packages lose custom bundle files and content hash doesn't cover them.

### Problem B: No path traversal protection

`_copy_bundle_into_project` (line 645–648) and `_load_bundle_files` (line 416–417) do `bundle_dir / bf.path` and `dest_dir / rel_path` without verifying the resolved path stays within the target directory. A malicious `manifest.json` with `"path": "../../../etc/passwd"` could read/write outside the sandbox.

### Files to Change

#### 1. `backend/app/services/package_service.py` — add path safety utility

New static method:

```python
@staticmethod
def _safe_bundle_path(base_dir: Path, relative: str, *, must_exist: bool = False) -> Path | None:
    """Resolve a bundle-relative path and verify it stays within base_dir.
    
    Returns the resolved Path, or None if the path is unsafe.
    """
    if not relative:
        return None
    # Reject absolute paths and parent traversal in raw form
    if relative.startswith("/") or relative.startswith("\\"):
        return None
    if ".." in Path(relative).parts:
        return None
    resolved = (base_dir / relative).resolve()
    if not str(resolved).startswith(str(base_dir.resolve()) + "/"):
        return None
    if must_exist and not resolved.exists():
        return None
    return resolved
```

#### 2. `backend/app/services/package_service.py` — use safe path in `_load_bundle_files`

Replace `file_path = bundle_dir / bf.path` (line 417) with:

```python
file_path = self._safe_bundle_path(bundle_dir, bf.path)
if file_path is None or not file_path.is_file():
    continue
```

#### 3. `backend/app/services/package_service.py` — use safe path in `_copy_bundle_into_project`

Replace `file_path = dest_dir / rel_path` (line 646) with:

```python
file_path = self._safe_bundle_path(dest_dir, rel_path)
if file_path is None:
    continue
```

#### 4. `backend/app/services/package_service.py` — support zip bundle reading

Add method `_load_bundle_files_from_zip`:

```python
def _load_bundle_files_from_zip(self, source: Path, manifest: PackageManifest) -> dict[str, str]:
    """Load text bundle files from a zip archive."""
    files: dict[str, str] = {}
    with zipfile.ZipFile(source, "r") as zf:
        for bf in manifest.bundle.files:
            zip_path = f"bundle/{bf.path}"
            safe = self._safe_bundle_path(Path("/"), bf.path)  # validate path shape
            if safe is None:
                continue
            try:
                info = zf.getinfo(zip_path)
            except KeyError:
                continue
            ext = Path(bf.path).suffix.lower()
            if ext not in _ALLOWED_BUNDLE_EXTENSIONS:
                continue
            if info.file_size > _MAX_BUNDLE_FILE_SIZE_BYTES:
                continue
            try:
                content = zf.read(zip_path).decode("utf-8")
                files[bf.path] = content
            except (UnicodeDecodeError, OSError):
                continue
    return files
```

#### 5. Update `_load_bundle_files` to dispatch

```python
def _load_bundle_files(self, source: Path, manifest: PackageManifest) -> dict[str, str]:
    if source.is_dir():
        return self._load_bundle_files_from_dir(source, manifest)
    if zipfile.is_zipfile(source):
        return self._load_bundle_files_from_zip(source, manifest)
    return {}
```

#### 6. Apply same safe-path checks to `_load_stored_bundle_files`

The `rglob` pattern is safe (it only returns real files under `bundle_dir`), but add the `_safe_bundle_path` check before `file_path.read_text()` as defense-in-depth.

---

## P2: Cleanup (Non-blocking, Deferrable)

### 1. `search_card_packages` response — strip `score`

In `manager_blueprint_tools.py:1102` search handler, remove `score` from returned items unless explicitly requested for debug.

### 2. `get_card_package_detail` — whitelist fields

Currently returns full `manifest.model_dump()` (line 1128). Change to a detail whitelist serializer that only exposes: title, summary, tags, executor requirements, bundle file names (not contents).

### 3. `resummarize_entry` — whitelist serializer

Audit the summarization path to ensure it doesn't leak internal fields from `PackageIndexEntry` or `PackageManifest`.

### 4. MCP manifest parsing — support per-item selection

When manifest has `mcpServers` as a nested object with multiple named entries, support selecting by `item.id` rather than always picking the first.

---

## Execution Order

```
Phase 0 (immediate, 1-2 sessions):
  1. P0-2: Manager package tool registration in server.js
  2. P0-1: Runtime sync — card dropdown persistence

Phase 1 (next, 1-2 sessions):
  3. P1-3: Path traversal + zip bundle support
  4. P1-2: Runtime source split fields
  5. P1-1: Runtime dependency resolver integration

Phase 2 (later, 1 session):
  6. P2 cleanups
```

**Rationale**: P0-2 is pure-additive (no existing behavior change), can be done independently. P0-1 touches frontend persistence semantics and should be verified end-to-end. P1-3 is the security fix and should not wait. P1-1 depends on understanding the `RuntimeDependencyResolverService` interface — needs a quick spike to confirm the resolver's resolve() signature.
