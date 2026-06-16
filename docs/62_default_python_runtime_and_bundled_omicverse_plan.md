# 62. Default Python Runtime Detection And Bundled Omicverse Plan

Status: review + implementation plan.

Date: 2026-06-16

Related:

- `docs/60_bundled_mamba_r_runtime_plan.md` (bundled mamba + R runtime mechanism
  this plan mirrors for Python)
- `docs/61_dependency_install_persistence_and_frontend_perf_review.md`

## Summary

The 0.4.5-beta deploy emitted a warning:

```text
No default Python runtime detected. Cards without an explicit python_runtime
may require manual runtime selection.
```

This review found four work items, all in one plan:

1. **Detection bug (Layer 1):** `detect_default_python_runtime` in
   `deploy_release.sh` only inspects a single conda base, while the backend
   runtime resolver searches all candidate bases. The two disagree.
2. **Sandbox/resolver hardening (Layer 1.5):** two integration gaps in
   `command_worker.py` that affect runtime execution correctness for envs
   outside the configured conda base and for micromamba-based envs.
3. **Bundled Python analysis runtime (Layer 2):** the bundled mamba root ships
   an R runtime but no Python analysis runtime. Add an optional `omicverse`
   env via online `mamba create`, mirroring the R runtime exactly.
4. **In-app runtime creation (Layer 3):** the system can only install into
   existing runtimes. Add a `create_runtime` capability so users can create new
   isolated Python/R environments from the UI, making the bundled mamba root a
   self-service runtime manager.

The packaging model for Layer 2 is "one payload, two install profiles" (minimal
vs full), selected by an install-time switch rather than two release artifacts.

## 1. Current Architecture

The install creates two separate conda environment trees:

| Tree | Path | Purpose | Built by |
|------|------|---------|----------|
| Base run env | `~/.local/share/blueprint-re/env` | Backend process itself (uvicorn + deps) | `install.sh` Phase 5, from `environment.yml` (python=3.13, nodejs, nginx, bwrap, git) |
| Bundled mamba root | `~/.local/share/blueprint-re/mamba` | User analysis runtimes (R/Python) | `install.sh` Phase 5b, copies `micromamba` + creates `envs/blueprint-re-r` |

The bundled mamba root contains only `micromamba` + `envs/blueprint-re-r`. It has
no `bin/python` and no `envs/omicverse`.

Confirmed working on a fresh machine (no change needed): Phase 4 extracts bundled
`micromamba` from payload (`install.sh:486`), Phase 5 uses it to build the base
run env, Phase 5b copies it to the bundled mamba root. A fresh machine needs no
pre-installed conda. Runtime enumeration (`_python_runtimes`/`_r_runtimes` at
`project_service.py:1082,1137`) auto-discovers any `envs/<name>`, so a new env
appears in the picker with no registration.

## 2. The Detection Bug (Layer 1)

### 2.1 Root cause

`deploy_release.sh:274-294` `detect_conda_base()` returns the **first** base with
a conda/micromamba binary. The bundled mamba root wins (it has `bin/micromamba`),
but has no `bin/python` and no `envs/omicverse`.

`detect_default_python_runtime()` (`:296-317`) receives only that one base and
searches `omicverse` -> `analysis` -> `base` inside it. All three miss ->
`BLUEPRINT_DEFAULT_PYTHON_RUNTIME` is empty -> warning.

Meanwhile `command_worker.py:257-267` `_resolve_conda_runtime` uses
`default_conda_base_candidates` (`config.py:27-44`), the **full** candidate list
including `~/miniforge3`. So the deploy-time detector and the run-time resolver
use **inconsistent base lists**.

### 2.2 Two affected scripts

The same three detect functions are **duplicated** in two scripts, and their
candidate orderings have already diverged:

| Script | detect_conda_base | detect_default_python_runtime | detect_default_r_runtime |
|--------|-------------------|-------------------------------|--------------------------|
| `scripts/deploy_release.sh` | `:274` | `:296` | `:319` |
| `scripts/install_blueprint_re.sh` | `:117` | `:262` | `:280` |

`deploy_release.sh` includes `BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX` as a
candidate; `install_blueprint_re.sh` does not. Fixing only one re-introduces the
divergence. Layer 1 must touch **both** scripts (or, preferably, extract a shared
`scripts/lib_runtime_detect.sh` that both `source`).

### 2.3 Fix

Extract a shared `conda_base_candidates()` function returning the full ordered
list. `detect_default_python_runtime` / `detect_default_r_runtime` iterate it to
find the first base containing `envs/<name>/bin/python` (or `Rscript`).
`detect_conda_base` keeps its single-base semantics (for `BLUEPRINT_EXECUTOR_CONDA_BASE`,
the solver source). The two responsibilities stay separated. Both scripts must
use the same shared implementation.

> **source 路径解析：** `deploy_release.sh` 由自解压安装器执行（payload
> 解压到临时目录后调用），`install_blueprint_re.sh` 由 repo 直接跑——两者
> 的工作目录不同，相对路径 `source` 会失败。`lib_runtime_detect.sh` 与调用
> 脚本的相对位置在两种场景下一致（都在 payload 的 `scripts/` 下），但必须用
> `BASH_SOURCE` 解析绝对路径：
> ```bash
> DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
> # shellcheck source=lib_runtime_detect.sh
> source "${DIR}/lib_runtime_detect.sh"
> ```
> 不要依赖 `$PWD` 或硬编码路径。

## 3. Sandbox/Resolver Hardening (Layer 1.5)

Two integration gaps in `command_worker.py` found during the fresh-machine
review. Neither blocks Layer 2 (omicverse lives under the bundled mamba root,
already covered), but both are correctness fixes for "any runtime path works".

### 3.1 Gap B — bwrap does not bind the resolved env_path

`command_worker.py:438-453` binds only `executor_conda_base` and
`executor_mamba_root_prefix`. A resolved `env_path` outside those two roots
(e.g. an env under `~/miniforge3` when conda_base is bundled mamba, or a
user-given absolute path) is not bound -> card fails in the sandbox.

**Fix:** after resolving `env_path`, bind it explicitly
(`--ro-bind <env_path> <env_path>`) in addition to the base-level binds.

### 3.2 Gap C — `_apply_conda_runtime` does not recognize micromamba

`command_worker.py:247`: `conda_bin = conda_base / "bin" / "conda"`. Bundled
mamba has no `bin/conda`, so the `conda run -p <env>` wrapper branch is skipped
and the env is activated via bare PATH injection. This loses `activate.d`
scripts (`LD_LIBRARY_PATH` etc.) for compiled-library envs.

**Fix:** align solver lookup with `find_conda_solver` (micromamba-first); use
`micromamba run -p <env> -a ""` when micromamba is the solver.

> **Flag note:** micromamba 2.8.0 (the bundled version) does **not** support
> `--no-capture-output` (that is a `conda run` / `mamba run` flag). The
> micromamba equivalent for passing stdout/stderr through unbuffered is
> `-a ""` (attach with empty stream spec = disable stream redirection).
> Implementation must verify the flag against the actual bundled version and
> fall back to bare PATH injection if the flag is rejected.

## 4. Bundled Omicverse Runtime (Layer 2)

### 4.1 Design: one payload, two install profiles

Ship one payload; select the profile at install time via an environment switch,
exactly like the existing R runtime mechanism (`BLUEPRINT_INSTALL_R_RUNTIME`):

| Profile | Switch | What gets built | Default python_runtime |
|---------|--------|-----------------|------------------------|
| Minimal | `BLUEPRINT_INSTALL_PYTHON_RUNTIME=0` (default) | base run env only | whatever Layer 1 detects (usually `base`) |
| Full | `BLUEPRINT_INSTALL_PYTHON_RUNTIME=1` | base run env + `omicverse` env via online `mamba create` | `omicverse` |

The payload carries only a spec file (`blueprint-re-python.yml`, a few KB). The
actual environment is created online at install time, identical to how
`blueprint-re-r.yml` works. Payload size does not grow.

### 4.2 Omicverse spec

New file `deploy/runtime/blueprint-re-python.yml`:

```yaml
name: omicverse
channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/bioconda
dependencies:
  - python =3.12
  - omicverse
  - scanpy
  - anndata
  - numpy
  - pandas
  - scipy
  - scikit-learn
  - matplotlib
  - seaborn
  - jupyter
  - ipykernel
```

- `name: omicverse` matches the `-n omicverse` used in `mamba create`, mirroring
  how `blueprint-re-r.yml` uses `name: blueprint-re-r` with `-n blueprint-re-r`.
- `python =3.12` (not 3.13): omicverse dependency closure is widest on 3.12.
- Heavy optional modules (numpyro, cell2location, cellpose, scvi-tools) left out
  to keep online create under ~20 min and ~4 GB; users add them via the runtime
  dependency installer after install.
- bioconda is included for Python too: scanpy and several of its dependencies
  live on bioconda, not just conda-forge.
- Channel mirrors match `blueprint-re-r.yml`.

### 4.3 Install-time provisioning

Mirror `provision_bundled_r_runtime` (`install.sh:630-682`) exactly: guard on
`BLUEPRINT_INSTALL_PYTHON_RUNTIME`, spec-hash + micromamba-version build marker
for up-to-date skip, `mamba create -n omicverse -f <spec>`, marker write.

Call site (`install.sh:715-717`) becomes:

```bash
if provision_bundled_mamba; then
  provision_bundled_python_runtime   # new
  provision_bundled_r_runtime
  provision_bundled_r_extras
fi
```

After Layer 1 is fixed, deploy auto-detects the freshly built `omicverse` as the
default Python runtime.

### 4.4 Build bundle change

`build_release_bundle.sh` (near the R yml copy at `:314-319`):

```bash
if [[ -f "${REPO_ROOT}/deploy/runtime/blueprint-re-python.yml" ]]; then
  cp "${REPO_ROOT}/deploy/runtime/blueprint-re-python.yml" "${BUNDLE_ROOT}/runtime/blueprint-re-python.yml"
fi
```

## 5. In-App Runtime Creation (Layer 3)

### 5.1 Motivation

The system can only install packages into an *existing* runtime. A user wanting
a fresh isolated environment (a clean Python 3.11 for a specific pipeline, or a
separate R env with conflicting package versions) has no in-app path and must
drop to a shell. Layer 3 adds a `create_runtime` capability so the bundled mamba
root becomes a first-class self-service runtime manager.

### 5.2 Current state

- Backend has no create-env capability. `InstallRuntimeDependenciesPayload`
  (`manager_blueprint_tools.py:159`) only accepts `runtime` (existing env name)
  + `packages`. No `mamba create` code path anywhere.
- Runtime enumeration auto-discovers any `envs/<name>`, so a newly created env
  appears in the picker with **no registration** — the missing piece is purely
  the creation action.
- The runtime dependency job infrastructure (background job, phase callbacks, SSE
  events, chip UI, resolver) is built around install-into-existing; creating an
  env reuses much of it with a different command.

### 5.3 Backend API

New endpoint under the internal manager-tools router:

```
POST /api/internal/manager-tools/projects/{project_id}/runtime-dependencies/create-runtime
```

Payload schema (`CreateRuntimePayload`, `manager_blueprint_tools.py`):

```python
class CreateRuntimePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ecosystem: Literal["python", "r"]       # drives default channels
    env_name: str                            # ^[A-Za-z][A-Za-z0-9_-]*$, <= 64 chars
    packages: list[str] = []                 # bare names only, grammar-checked
    python_version: str | None = None        # e.g. "3.12"; None = solver default
    r_version: str | None = None             # e.g. "4.4"; ecosystem=="r" only
    auto_select: bool = False                # write new env into project runtime prefs
    timeout_seconds: int = 1200              # creation can be slow; mirrors R extras
    source: dict = {}                        # session_id for chat receipt routing
```

> **Pydantic mutable default：** `source: dict = {}` 在 Pydantic v2 下会自动
> deep-copy，不会共享实例，是安全的。如果团队偏好 `Field(default_factory=dict)`
> 风格，落地时按本地惯例写即可，行为等价。

Service method `ManagerBlueprintTools.create_runtime(project_id, payload, session_id)`:


1. Validate `env_name` grammar (`^[A-Za-z][A-Za-z0-9_-]*$`) and reject shell-danger
   chars (reuse `_contains_shell_danger` / `is_registry_fallback_action_safe`).
2. Resolve the target base = bundled mamba root (`executor_mamba_root_prefix`).
3. Refuse if `mamba/envs/<env_name>` already exists (return
   `error_code: runtime_already_exists`).
4. Resolve the solver via `find_conda_solver(base)` (micromamba).
5. Build the create command internally — never from user text:
   - `micromamba create -y -n <env_name> -p <mamba>/envs/<env_name>`
   - Channels: `conda-forge` + `bioconda` for **both** ecosystems (scanpy and
     many bioinformatics Python packages live on bioconda), from mirror presets.
   - Base dep: `python=<python_version>` or `r-base=<r_version>`.
   - Append grammar-checked bare package names from `packages`.
6. Submit as a runtime dependency job. The job is routed to
   `_create_runtime_sync` (see §5.4 for the routing mechanism).
7. On success: the env auto-appears in the picker (enumeration). If
   `auto_select` is true, write the new env name into the project's
   `runtime_preferences` (`python_runtime` or `r_runtime` per ecosystem) via
   `update_project_runtime_preferences`. When `auto_select` is false (default),
   only refetch the environment endpoint so the picker shows the new option; the
   user selects it explicitly.

### 5.4 Job model and routing

`RuntimeDependencyJobService` is reused, but two changes are needed because the
current `submit` (`runtime_dependency_job_service.py:79-86`) hardcodes
`task_type="runtime_dependency_install"` and the handler is passed positionally
by the caller.

**Routing approach (chosen: new task_type + handler dispatch):**

1. Add a `task_type` parameter to `submit(project_id, payload, handler, *, task_type="runtime_dependency_install")`.
   `create_runtime` passes `task_type="runtime_dependency_create"`.
2. The caller passes the handler (`_create_runtime_sync`) as the `handler`
   argument, exactly as `install_runtime_dependencies` passes
   `_install_runtime_dependencies_sync` today (`manager_blueprint_tools.py:1306`).
   No internal dispatch table is needed — the handler binding is already
   caller-driven.
3. The background task record (`BackgroundTaskService`) stores the `task_type`
   so workboard / UI can distinguish install vs create jobs if needed.

`_create_runtime_sync` mirrors `_install_runtime_dependencies_sync` but builds a
`micromamba create` command. Phases: `waiting_for_runtime_lock` ->
`building_command` -> `launching_subprocess` -> `running_subprocess` -> terminal.
The `DependencyJobChip` and SSE event handler need no changes — they are
phase-driven and already handle these phase names.

The `installer_plan` concept does not apply (there is no resolver probe for
"create"); the resolver is skipped and the command is built directly from the
validated payload.

### 5.5 Constraints and safety

| Constraint | Rule |
|------------|------|
| env_name grammar | `^[A-Za-z][A-Za-z0-9_-]*$`, max 64 chars |
| env_name reserved | reject exact `base`, `__system__`, and any name matching `^blueprint-re-` (protects bundled envs: `blueprint-re-r`, future `blueprint-re-python`, etc.) |
| package names | bare names only; reject source specs (URLs, GitHub, tarballs) and shell-danger chars; reuse `is_registry_fallback_action_safe` grammar |
| overwrite | refuse if env already exists |
| target base | always the bundled mamba root (`executor_mamba_root_prefix`); user cannot target an arbitrary path. Rationale: (a) bwrap binding surface stays minimal and predictable — the mamba root is already bound as a whole; (b) runtime enumeration (`_python_runtimes`/`_r_runtimes`) scans the candidate base list deterministically, and the bundled mamba root is always in that list; (c) it prevents users from accidentally polluting system conda installs. Users with an existing miniforge3 env can still select it in the picker — they just cannot *create into* it via this API. |
| python_version | free-form bare version string (`^\d+\.\d+(\.\d+)?$`) or null |
| r_version | same grammar; only when `ecosystem == "r"` |
| timeout | default 1200s, max 1800s. omicverse + scanpy + scipy from conda-forge/bioconda typically takes 10-20 min on the tsinghua mirror; mirror jitter can push it to 30 min. 1200s default aligns with the observed R extras provisioning time. |

### 5.6 Frontend

Two entry points:

1. **Advanced settings panel** (`components/advanced/AdvancedPanels.tsx`): a
   "新建运行环境" form — ecosystem radio, env_name input, optional version,
   package list, an `auto_select` checkbox (default off), submit button. On
   success, refetch the environment endpoint so the new env appears in the
   picker. If `auto_select` was checked, the backend writes it into project
   runtime prefs (§5.3 step 7) and the frontend refetches runtime preferences
   too.
2. **Manager tool** `create_runtime` (chat-driven): the manager can call it when
   a user asks "帮我建一个叫 xxx 的 Python 3.11 环境". Register the tool in
   `manager-agent/src/server.js` tool registry with labels
   `create_runtime: { active: "正在创建运行环境", done: "已创建运行环境" }`.

The creation runs as a background job, so the existing `DependencyJobChip`
("正在处理 N 个依赖任务") covers the in-progress state with no new chip logic.

### 5.7 Files touched (Layer 3)

| File | Change |
|------|--------|
| `backend/app/services/runtime_dependency_job_service.py` | Add `task_type` parameter to `submit` (default `runtime_dependency_install`) |
| `backend/app/services/manager_blueprint_tools.py` | `CreateRuntimePayload` model + `create_runtime` method + `_create_runtime_sync` handler |
| `backend/app/api/manager_tools.py` | `POST /runtime-dependencies/create-runtime` endpoint + `_guard_mutation` |
| `manager-agent/src/server.js` | register `create_runtime` tool + labels |
| `frontend/components/advanced/AdvancedPanels.tsx` | "新建运行环境" form with `auto_select` checkbox |
| `frontend/lib/api.ts` | `createRuntime` client method |
| `backend/tests/test_create_runtime.py` | new: grammar validation, overwrite refusal, command building, success/failure paths |

## 6. Implementation Plan

Implementation order (each layer can ship independently; later layers depend on
earlier ones being correct):

### Layer 1 — detection bug fix

| # | File | Change |
|---|------|--------|
| 1 | `scripts/lib_runtime_detect.sh` (new) | Shared `conda_base_candidates()` + multi-base `detect_default_python_runtime` / `detect_default_r_runtime`; both scripts source this |
| 2 | `scripts/deploy_release.sh:274,296,319` | Remove local detect definitions, `source` the shared lib |
| 3 | `scripts/install_blueprint_re.sh:117,262,280` | Same: remove local detect definitions, `source` the shared lib |

Verify: host with `~/miniforge3/envs/omicverse` + bundled mamba root auto-detects
`omicverse` with no warning via **both** install paths; host with only base
detects `base`.

### Layer 1.5 — sandbox/resolver hardening

| # | File | Change |
|---|------|--------|
| 4 | `backend/app/workers/command_worker.py:453` | Bind resolved `env_path` explicitly into bwrap |
| 5 | `backend/app/workers/command_worker.py:247` | Solver lookup micromamba-first; use `micromamba run -p <env> -a ""` (verify flag against bundled 2.8.0; fall back to bare PATH injection if rejected) |

Verify: a runtime at an absolute path outside conda_base/mamba_root executes in
sandbox; a bundled-mamba env activates with full `activate.d` scripts.

### Layer 2 — bundled omicverse

| # | File | Change |
|---|------|--------|
| 6 | `deploy/runtime/blueprint-re-python.yml` | New omicverse core spec, `name: omicverse` (§4.2) |
| 7 | `scripts/install.sh:590` | `BLUEPRINT_INSTALL_PYTHON_RUNTIME=0` default + `provision_bundled_python_runtime` |
| 8 | `scripts/install.sh:715` | Call `provision_bundled_python_runtime` after `provision_bundled_mamba` |
| 9 | `scripts/install.sh:86` | Document `BLUEPRINT_INSTALL_PYTHON_RUNTIME` in usage block |
| 10 | `scripts/build_release_bundle.sh:314` | Copy `blueprint-re-python.yml` into `runtime/` |

Verify: `BLUEPRINT_INSTALL_PYTHON_RUNTIME=1 bash <installer>.sh` creates
`mamba/envs/omicverse` with working `python -c "import omicverse"`; deploy
auto-sets `BLUEPRINT_DEFAULT_PYTHON_RUNTIME=omicverse`.

### Layer 3 — in-app runtime creation

| # | File | Change |
|---|------|--------|
| 11 | `backend/app/services/runtime_dependency_job_service.py:79` | Add `task_type` parameter to `submit` (default `runtime_dependency_install`) |
| 12 | `backend/app/services/manager_blueprint_tools.py` | `CreateRuntimePayload` + `create_runtime` + `_create_runtime_sync` (§5.3-5.5) |
| 13 | `backend/app/api/manager_tools.py` | `POST /runtime-dependencies/create-runtime` endpoint |
| 14 | `manager-agent/src/server.js` | Register `create_runtime` tool + labels |
| 15 | `frontend/components/advanced/AdvancedPanels.tsx` | "新建运行环境" form with `auto_select` checkbox |
| 16 | `frontend/lib/api.ts` | `createRuntime` client method |
| 17 | `backend/tests/test_create_runtime.py` | Grammar/overwrite/command/success/failure tests |

Verify: create a `test-py311` Python env from the advanced panel; it appears in
the runtime picker; a card bound to it executes in sandbox; refuse to overwrite
an existing env; reject bad names/packages; `auto_select=true` writes into
project runtime prefs.

## 7. Out Of Scope

- Pre-downloading omicverse into the payload (`--with-python-cache`): rejected.
  3-4 GB payload bloat + version staleness outweigh the offline benefit.
- Bundling the full 11 GB production omicverse env: rejected for the same reason.
- Changing the base run env (`environment.yml`): out of scope; that env is for
  the backend process, not user analysis.

## Appendix — install command reference

| Goal | Command |
|------|---------|
| Minimal install (base only) | `bash <installer>.sh` |
| Full install (omicverse + R extras) | `BLUEPRINT_INSTALL_PYTHON_RUNTIME=1 BLUEPRINT_INSTALL_R_EXTRAS=1 bash <installer>.sh` |
| Upgrade (keeps existing envs) | `bash <installer>.sh --upgrade` |
