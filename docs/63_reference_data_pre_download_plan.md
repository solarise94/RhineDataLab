# 63. Reference-Data Pre-Download And Dependency-Resolver UX Upgrade Plan

Status: design + implementation plan (no code written yet).

Date: 2026-06-19

Related:

- `docs/58_card_library_blueprint_deck_design.md` (card library + blueprint model
  that declares `reference_assets`)
- `docs/60_bundled_mamba_r_runtime_plan.md` (offline cache pattern + extras gap)
- `docs/61_dependency_install_persistence_and_frontend_perf_review.md`
  (runtime dependency job persistence + frontend perf)
- `docs/62_default_python_runtime_and_bundled_omicverse_plan.md`
  (default runtime + resolver/worker coupling)

## Summary

Two coupled problems were raised together and belong in one plan because they
share the same resolver/job machinery and the same frontend progress surface:

1. **Reference databases (GTF / GFF / TCGA / GENCODE / Ensembl / annotation
   DBs) have no pre-download path.** There is no code anywhere in the backend
   that fetches these files. The executor is explicitly forbidden from
   installing or fetching (`command_worker.py:1597, 1662-1666`); on a missing
   reference it can only `fail`. A `ReferenceDataService` registry already
   exists for *storing* such files (content-addressed, sha256-deduped), but it
   is a warehouse, not a procurement office: it has `register_local` /
   `register_upload` but no `fetch_and_register`. Cards can declare reference
   dependencies via `ReferenceAssetRef`, but that model carries only a `ref_id`
   pointing at an already-registered entry — no source URL, no mirror, no
   sha256.
2. **The dependency resolver UI "just spins."** During an active install the
   frontend shows a single indeterminate spinner (`Loader2`) with static text
   `依赖处理中...`, discarding the phase enum and log tails the backend already
   sends. There is no progress bar, no package count, no network-speed, no
   elapsed time. Users perceive this as "stuck."

The plan wires both into the **existing** runtime-dependency resolver + job
service rather than building parallel systems, and adds an optional **egress
proxy** as a sibling of the existing mirror-preset mechanism.

## Architecture Ground Truth (what exists today)

This section records the facts the plan depends on, so reviewers do not need to
re-derive them.

### Reference data — storage exists, fetching does not

- `backend/app/services/reference_data_service.py:89` — `ReferenceDataService`,
  storage at `{data_root}/_system/reference-data/{ref_id}/{meta.json,data/<file>}`,
  content-addressed by sha256. Methods: `register_local` (L187, approved-roots
  only), `register_upload` (L199, streamed through a registry temp file),
  `resolve(ref_id)` (L326), `download` (L330, serves already-stored files only).
  Security contract (docstring L17-27): copies never moves/symlinks, traversal
  rejected, symlink-escape rejected.
- `ReferenceDataKind = Literal["gtf","fasta","index","annotation","table","other"]`
  (L43) — the kind taxonomy already covers the use cases.
- `backend/app/api/reference_data.py` — REST surface (`GET /`, `POST /` upload,
  `POST /from-path`, `GET/{ref_id}`, `DELETE/{ref_id}`, `GET/{ref_id}/download`).

### Blueprint model declares references but cannot name a source

- `backend/app/models/card_blueprint.py:126` —
  `ReferenceAssetRef { ref_id, role, required, description }`. No source/mirror
  field. The docstring states the intent: "env dependency, not a consumed input
  slot."
- `CardBlueprint.reference_assets: list[ReferenceAssetRef]` (L174).
- `backend/app/services/card_library_service.py:1099-1170` — at instantiation
  the loop `ref_service.resolve(ref.ref_id)` resolves already-registered files;
  on `ReferenceDataError` it appends a blocker if `required`. This is the
  single resolve hook today.
- `card_library_service.py:344-429` `_build_blueprint_from_card` — drops
  `reference_assets` when generalizing a card into a blueprint. Round-trip gap
  to fix alongside the feature.

### Reference paths reach the executor through existing plumbing

- `worker_service.py:2083-2092` `_merge_executor_context` — card `references`
  merge into the default worker references (append + dedup by path).
- `command_worker.py:182-185` — `BLUEPRINT_REFERENCE_PATHS` env var built from
  `template_metadata["reference_paths"]`.
- `command_worker.py:1568, 1652` — references rendered into the prompt as
  `- Reference: {path} ({type})`.
- `command_worker.py:394` — relies on the `--ro-bind / /` host-root catch-all.
  Not explicitly in `readonly_binds` (L447) or `sandbox_plan.json` (L631). If
  the host-root catch-all is ever tightened, references must be added
  explicitly.

### Runtime dependency resolver + job — the template to reuse

- `backend/app/services/runtime_dependency_resolver_service.py` — pure planner
  ("runs before the installer creates a background job … without mutating any
  runtime state"). `resolve()` (L366-602) classifies packages and produces a
  `RuntimeDependencyResolutionPlan` with `installable` + `blocked`.
- `backend/app/services/runtime_dependency_job_service.py` — `RuntimeDependencyJobService`
  (ThreadPoolExecutor, max 2 workers, per-runtime lock at `runtime_locks[(project_id, runtime)]`).
  Persists to `{project}/chat/runtime_dependency_jobs.json` (L452). Phases:
  `queued → waiting_for_runtime_lock → building_command → launching_subprocess
  → running_subprocess → succeeded/failed`. Watchdog (L628-712) reconciles
  orphaned jobs after backend restart.
- `backend/app/services/manager_blueprint_tools.py:1206` —
  `install_runtime_dependencies` is the entry point: in-flight duplicate
  suppression, terminal-failure cooling, resolver-first gating, then
  `runtime_dependency_job_service.submit(handler=_install_runtime_dependencies_sync)`.
- `worker_service.py:293` — `runtime_dependency_blocker` gates a run on pending
  dependency jobs. This is the existing run-pause hook.

### Dependency-install progress — backend sends more than UI uses

- `runtime_dependency_job_service.py:25-50` — `RuntimeDependencyJob` dataclass
  fields. Notably: `stdout_tail`, `stderr_tail` (L49-50) declared and persisted
  but **populated only at terminal**; `last_stdout_at`, `last_stderr_at`
  (L47-48) **declared, persisted, round-tripped on load, never written** —
  dead placeholders. No `progress`/`percent`/`bytes`/`rate` field exists.
- `_make_phase_callback` (`runtime_dependency_job_service.py:356-378`) accepts
  only `phase`, `child_pid`, `command_preview`, `log_path`. Does a full
  atomic-write JSON + SSE emit per call — too expensive at line rate.
- `_emit_project_event` (L380-450) — SSE payload carries `phase`, `job_status`,
  and on terminal only also `stdout_tail`/`stderr_tail`/`error_code`/`message`.
  Mid-run events carry phase only.
- `_run_dependency_command` (`manager_blueprint_tools.py:1946-2049`) — the
  **single chokepoint** for all installers (conda/pip/CRAN/Bioconductor all
  delegate here). Uses blocking `subprocess.run(command, capture_output=True)`
  (L1978-1986), so output is fully buffered until exit — not tailable, not
  metered. Comment at L1965-1967 already flags switching to `Popen` as "future
  P1 work."
- `_install_from_plan` (L1824), `_run_pip_install` (L2051),
  `_run_r_registry_install` (L2131) all route through `_run_dependency_command`,
  so instrumenting the chokepoint covers every installer.

### Frontend consumes almost none of it

- `frontend/components/dependency/DependencyJobChip.tsx` — the single chip.
  Active chip (L177-197) shows `Loader2` (size 14, `.spinning` 1s linear
  infinite) + at most `packages[0]`, else static `依赖处理中...` /
  `正在处理 N 个依赖任务`. `phase`/`status` used only to bucket active vs
  terminal; `stdout_tail`/`stderr_tail`/`started_at`/`finished_at` unused.
- Mount points: `frontend/components/layout/SideNav.tsx:396` (desktop),
  `frontend/components/layout/ProjectWorkspace.tsx:859` (mobile floating).
- Data path: SSE `EventSource` → `/projects/{id}/events`
  (`ProjectWorkspace.tsx:265`), events with `reason ===
  "runtime_dependency_job_changed"` drive `registerDependencyJob` /
  `updateDependencyJob` in `frontend/lib/stores/workspace-ui-store.ts`
  (`DependencyJobChipState` L29-40, actions L85-88, L350-411). REST poll every
  5s (`DependencyJobChip.tsx:114-149`) only acts on terminal.
- Phase enum (8 values) defined in three places:
  `DependencyJobChip.tsx:33-41`, `workspace-ui-store.ts:398-405`,
  `ProjectWorkspace.tsx:388-392` — keep these in sync.
- `frontend/app/globals.css:4268,4272,4519-4569` — spinner + chip styles.

### Egress proxy — none today, but a clean insertion point

- No `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` is read, set, or surfaced anywhere in
  backend Python. Word "proxy" appears nowhere in `backend/app` except an
  allowlist.
- `command_worker.py:560-570` — the bwrap sandbox env allowlist **forwards** the
  six host proxy vars into the executor sandbox (passthrough, not a setter).
- All install subprocesses inherit `os.environ`:
  - `_run_dependency_command` (L1978) `env=run_env`, where `run_env` is `None`
    for conda/pip → inherits `os.environ`.
  - `_dependency_subprocess_env` (L3053-3060) for R non-conda starts from
    `dict(os.environ)` and only prepends `PATH`.
  - Conda solver probes (`runtime_dependency_resolver_service.py:670,1265,1340,1546`;
    `manager_blueprint_tools.py:3147,3170`) call `subprocess.run` with no
    `env=` → inherit `os.environ`.
- **Consequence:** injecting `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` into the
  backend's `os.environ` makes pip, conda, mamba, and R's `download.file` use
  the proxy **with zero changes to subprocess calls.**
- The catch: pydantic-settings reads env into the `Settings` model but does
  **not** push values back to `os.environ`. One new startup step
  (re-export proxy fields to `os.environ`) is required.
- Node `manager-agent` `fetch` (`server.js:456-479`, `:3279-3304`) does **not**
  honor proxy env. Tavily websearch (only when `MANAGER_WEBSEARCH_ENABLED` +
  `TAVILY_API_KEY`) would need explicit `undici.ProxyAgent` wiring — the one
  place proxy env alone is insufficient.
- `Settings` network fields today (`config.py:127-134`):
  `executor_conda_base`, `executor_mamba_root_prefix`, `executor_mambarc`,
  `cran_mirror`, `bioconductor_mirror`, `pypi_mirror`. **No** `conda_channel`
  field (channels come from `.mambarc`); **no** `pip_index_url` field.
- Mirror-preset system (the sibling a proxy setting should follow):
  `deploy/runtime/mirror-presets/mirror_env.sh` defines `tsinghua`/`ustc`/
  `default` presets (conda base url, CRAN, Bioconductor, PyPI, `.mambarc`).
  `scripts/install_blueprint_re.sh:38-60` `_expand_mirror_preset()` sources it,
  honors user overrides (`_USER_*_MIRROR_SET`). Tests:
  `scripts/test_provisioning.sh:115-254`.
- Deploy whitelists (the documented surface for new settings):
  `scripts/deploy_user_systemd.sh:653-691` and `scripts/deploy_release.sh:526-575`
  write `backend.env`; both carry a comment that the whitelist *is* the runtime
  contract. Proxy vars must be added here, mirroring `BLUEPRINT_CRAN_MIRROR`.

## Design Principles

1. **Reuse, do not fork.** Reference-data download rides the existing
   resolver → job → SSE pipeline. No new job service, no new SSE channel, no
   new persistence file. The dependency chip already exists on every screen.
2. **Executor stays a pure consumer.** It binds an already-prepared
   environment. Downloading inside the bwrap sandbox would (a) violate the
   explicit "do not fetch" contract and (b) land in the ephemeral
   `run_dir/cache`, which is discarded per run — defeating reuse.
3. **Environment-level storage, not run-level.** Downloads land under
   `{data_root}/_system/reference-data/`, which is cross-project/cross-run.
   This is what makes "download once, reuse" true.
4. **Proxy is a sibling of mirror preset.** Same deploy-time shape, same
   override semantics, same whitelist surface.
5. **Progress is incremental, not all-or-nothing.** Phase-stepper +
   elapsed-time ship first (frontend-only, uses existing data). True byte/speed
   metering ships second (needs Popen + new fields). The chip never blocks on
   the second phase to deliver the first.

## Work Items

### Layer A — Blueprint can declare a reference source

**Goal:** cards can say "I need hg38 GENCODE GTF, fetch from these mirrors,
expected sha256 X."

**A1. `ReferenceDataSourceSpec` model** — new, in
`backend/app/models/card_blueprint.py` near `ReferenceAssetRef`:

```python
class ReferenceDataSourceSpec(BaseModel):
    url: str
    mirrors: list[str] = Field(default_factory=list)  # tried in order on failure
    sha256: str                                         # required; download rejected if mismatch
    kind: ReferenceDataKind = "other"                  # gtf|fasta|index|annotation|table|other
    filename: str | None = None                        # override stored filename
    size_hint: int | None = None                       # bytes, for progress baseline
```

No auth/token field in v1 — sources are no-auth mirrors only (see D-5). When
auth-gated sources (GDC/TCGA) enter scope, add a `headers` field then; do not
ship a dead hook now.

**A2. Extend `ReferenceAssetRef`** (L126):

```python
class ReferenceAssetRef(BaseModel):
    ref_id: str | None = None          # now optional
    role: str
    required: bool = True
    description: str | None = None
    source: ReferenceDataSourceSpec | None = None   # NEW
```

Semantics:
- `ref_id` set → use already-registered file (current behavior, unchanged).
- `ref_id` absent + `source` set → eligible for pre-download; `ref_id` is
  back-filled after successful fetch.
- both absent + `required=True` → block (current behavior).
- both absent + `required=False` → optional, skip.

**A3. Fix the round-trip gap.** `_build_blueprint_from_card`
(`card_library_service.py:344-429`) must emit `reference_assets` so generalized
cards keep their reference dependencies.

**A4. Regenerate schemas.** Any Pydantic model change requires
`scripts/generate_backend_schemas.py` per AGENTS.md.

**Non-goals for Layer A:** no automatic URL → registry population yet (that is
Layer C). The model just *enables* declaring a source.

---

### Layer B — `ReferenceDataService` can fetch

**Goal:** add the missing "procurement" method, reusing the existing
sha256-dedup + copy-in contract.

**B1. `fetch_and_register(source: ReferenceDataSourceSpec) -> ReferenceDataMeta`**
in `backend/app/services/reference_data_service.py`. Contract:

1. Build candidate URL list: `[source.url, *source.mirrors]`.
2. **Disk pre-check (before any network):** if `source.size_hint` is set,
   compare `shutil.disk_usage(registry_temp_root).free` against `size_hint +
   headroom`. Fail fast with a clear `insufficient_disk_space` error rather
   than downloading for 30 minutes and hitting `ENOSPC`. Without `size_hint`,
   skip (cannot know); the streaming write still fails cleanly on `ENOSPC`
   mid-download and cleans up the temp.
3. For each URL, stream-download to a **registry-controlled temp file** under
   the registry root (same security posture as `register_upload` — never let
   an arbitrary host path land).
4. Compute sha256 incrementally as bytes arrive. Verify against
   `source.sha256` on completion; on mismatch delete the temp and try the next
   URL.
5. On success, hand the temp file to the existing `_ingest` (L232-304), which
   sha256-dedups against the registry and `shutil.copy2`s into place. The
   temp is always cleaned up (success or failure).
6. Honor proxy + timeout + mirror settings from `Settings` (Layer D). No
   per-request auth headers in v1 (sources are no-auth mirrors only, D-5).

**B1-retry. Retry budget (explicit, to avoid ambiguity at implementation
time):**

- `reference_download_timeout_s` is **per attempt**, not total. A 2 GB file on
  a slow mirror gets the full timeout budget each attempt, not a slice.
- `reference_download_retries` is the **total attempt count across the whole
  URL list**, not per-URL. With the default of 2 and a 3-URL list, that means
  "try URL 1, on failure try URL 2, stop" — *not* "try each URL twice." This
  favors mirror diversity over hammering one flaky host.
- A **sha256 mismatch is terminal for that URL** (the mirror is serving wrong
  or corrupted content) and advances to the next URL immediately, without
  consuming a retry slot — retries are for transport failures (timeout,
  connection reset, 5xx), not content failures.
- **No mirror blacklist persistence in v1.** A mismatched mirror is skipped
  for *this* download only; it is retried fresh on the next download job.
  Persisting a per-mirror reputation would be a later enhancement and is not
  needed for correctness (the sha256 check is the safety net).

**B2. Transport choice — known limitation on resume.** Use stdlib
`urllib.request` with a custom `Opener` + `ProxyHandler` (from `Settings`,
Layer D), to stay dependency-free (consistent with the rest of the backend,
which uses no `requests`/`httpx`).

**Resume is scoped out of v1.** Real HTTP resume (`Range` + `If-Range` +
ETag validation + appending to a partial file + resuming the sha256 hash
computation) is non-trivial to implement correctly with bare `urllib`, and
doing it wrong (resuming against a mirror whose content changed) is worse than
not doing it. v1 behavior: **a failed download restarts from byte zero on the
next URL.** This is acceptable because (a) the job has a retry budget so it
will eventually try a faster mirror, (b) GTF-scale files (~50 MB) restart in
seconds, and (c) the truly large case (TCGA) is deferred anyway (D-5). If
resume becomes a real pain point, the fix is to introduce `httpx` or `aria2`,
not to hand-roll `If-Range` — note this as a follow-up, do not half-implement.

**B3. No `delete` semantics change.** A downloaded file is registered like any
other; `delete` removes it per the existing contract. This is fine: downloads
are cached, not leased.

**Non-goals for Layer B:** no on-demand fetching from the executor. The
executor never calls this.

---

### Layer C — Wire fetching into the resolver → job pipeline (reuse, not new)

**Goal:** when a blueprint with a `source`-only `reference_assets` entry is
instantiated, the missing file is downloaded in the background and the run
pauses gracefully until ready — same UX as package installs today (and, after
Layer F, much better UX).

**C1. Planning: extend the instantiation resolve loop**
(`card_library_service.py:1099-1170`). When `ref_service.resolve(ref.ref_id)`
raises `ReferenceDataError`:
- if `ref.source` is set → do not block immediately; instead record a
  "pending-download" reference descriptor (url/mirrors/sha256/kind/role).
- if `ref.source` is absent and `required=True` → existing block behavior.
- if absent and `required=False` → skip.

**C2. Execution: route through the existing job service with a separate
handler.** A reference download is a dependency job, but its execution path is
fundamentally different from a package install: it is Python `urllib` streaming
+ sha256 verification, not a subprocess shell command. Entangling the two with
an `if/else` inside `_install_runtime_dependencies_sync` would couple
unrelated concerns and make the install handler harder to reason about.

Instead, register a **separate handler** and select it at submit time based on
the job payload shape:

- `submit(handler=_install_runtime_dependencies_sync, ...)` for package plans
  (unchanged).
- `submit(handler=_download_reference_asset_sync, ...)` for reference
  descriptors.

The job service (`RuntimeDependencyJobService`) already takes a handler
callable in `submit(...)` (`runtime_dependency_job_service.py:175` calls the
handler in `_run`), so this is a natural use of the existing extensibility
point — no change to the dispatch core. Both handlers share the service's
per-key locking, in-flight dedup, terminal cooling, persistence, watchdog, and
SSE channel. The new handler is thin:

```python
def _download_reference_asset_sync(self, project_id, payload, phase_callback):
    source = ReferenceDataSourceSpec(**payload["source"])
    phase_callback("launching_subprocess", command_preview=["fetch", source.url])
    service = ReferenceDataService(self.settings)
    meta = service.fetch_and_register(source)   # B1: disk-check → stream → sha256 → ingest
    phase_callback("running_subprocess", command_preview=["verify", source.sha256])
    return {"ref_id": meta.ref_id, "sha256": meta.sha256, "kind": meta.kind}
```

Reuse rationale (vs. a new job *service*) stands:
- per-key locking prevents double-download of the same GTF (the lock key is
  `source.sha256`, analogous to the per-runtime lock for package installs);
- in-flight dedup + terminal cooling already exists;
- the SSE channel and frontend chip already consume these jobs;
- one persistence file, one restart-reconciliation path.

The dedup keys in `find_duplicate_in_flight` /
`find_duplicate_terminal_failure`
(`runtime_dependency_state_service.py:86-100`) extend to key on
`source.sha256` for reference jobs (vs `runtime + sorted_pkgs` for package
jobs) so the same GTF is never re-downloaded.

**C3. Gating.** `worker_service.py:293` `runtime_dependency_blocker` is
already the run-pause hook; extend it to also treat pending reference-download
jobs as blockers. The run resumes automatically when the job terminal event
fires.

**C4. Sandbox exposure.** Downloaded files already live under
`{data_root}/_system/reference-data/`, reachable via the `--ro-bind / /`
catch-all (`command_worker.py:394`). For durability against future tightening,
also add the registry root to `readonly_binds` (L447) and record it in
`sandbox_plan.json` (L631). `BLUEPRINT_REFERENCE_PATHS` (L182) and the prompt
`- Reference:` lines (L1568) already carry the resolved paths — **no executor
logic change.**

**C5. Concurrency: downloads and installs need separate worker budgets.** The
job service runs a single `ThreadPoolExecutor(max_workers=2)`
(`runtime_dependency_job_service.py:57-64`). If reference downloads share that
pool naively, a 2 GB download occupies a worker slot for minutes and can
starve package installs for the same project — the inverse failure mode is
equally bad (two slow installs blocking a quick GTF fetch).

v1 policy: **two semaphores over one shared pool.** Keep the single
`ThreadPoolExecutor` (no new thread pool — that would split the watchdog and
persistence locking), but gate entry into the handler by *kind*:
- `max_install_workers = 2` (current behavior, unchanged).
- `max_download_workers = 1` (downloads are I/O-bound and network-limited;
  parallelism rarely helps and multiplies mirror load).

A handler acquires its kind's semaphore before doing real work and releases it
on completion/cancel. Net effect: at most 2 installs + 1 download run
concurrently, so a download never blocks both install slots, and two installs
never block the download slot. The per-key lock (C2) still prevents two jobs
fetching the *same* sha256. This is a small change to `_run`
(`runtime_dependency_job_service.py:175`): add the two semaphores and acquire
the right one by payload kind.

Tuning note: `max_download_workers=1` is deliberately conservative for v1. If
real usage shows a single project routinely downloading several distinct GTFs,
raise it — but start tight, because the user's stated pain point is
*perceived* stuckness, and spreading bandwidth across parallel downloads makes
each one slower.

**C6. Cancellation.** Long downloads make cancellation more valuable than for
package installs. The current job service has no first-class cancel; it relies
on terminal detection via the watchdog. For v1, add a **best-effort cancel**:

- The download handler (`_download_reference_asset_sync`) holds the
  `urllib` response handle on `self` (or in a cancel-handle registry keyed by
  `job_id`).
- A new `cancel(job_id)` on the job service sets a `cancelled` flag and calls
  `response.close()` / raises inside the streaming loop, which aborts the
  socket promptly.
- The handler catches the abort, deletes the partial temp file (B1 step 5
  cleanup), and marks the job `failed` with a `cancelled` reason — *not*
  `succeeded`. This matters for the terminal-cooling dedup
  (`runtime_dependency_state_service.py:361-363`): a cancellation must be
  retryable, not cooled.
- Surface this via the existing REST surface — extend the job-status poll
  endpoint (`manager_tools.py:673`) with a `POST .../cancel` sibling, or reuse
  the delete flow. Wiring a cancel button into the frontend chip is a small
  follow-up after F1.

This is best-effort: if the backend restarts mid-download, the partial temp is
orphaned and cleaned by the watchdog's orphan reconciliation
(`runtime_dependency_job_service.py:628-712`). The handler does not attempt to
resume (B2).

**Non-goals for Layer C:** TCGA GDC token-based bulk download is explicitly
deferred (D-5). GENCODE/Ensembl/no-auth mirrors are in scope.

---

### Layer D — Egress proxy for all outbound downloads

**Goal:** let users configure an HTTP/HTTPS proxy (corporate egress, China
mirror acceleration) and curate reference-source mirrors, as a sibling of the
existing package-mirror preset.

**D1. New `Settings` fields** (`backend/app/core/config.py`, env prefix
`BLUEPRINT_`):
- `http_proxy: str = ""`
- `https_proxy: str = ""`
- `no_proxy: str = ""`
- `reference_mirror_preset: str = ""` (empty = none; named presets resolved at
  Layer D4)
- `reference_download_timeout_s: int = 1800`
- `reference_download_retries: int = 2`

**D2. Re-export to `os.environ` at startup.** A backend startup hook (in
`app/main.py` or a small `app/core/proxy.py`) pushes the three proxy fields
into `os.environ["HTTP_PROXY"]`/`HTTPS_PROXY`/`NO_PROXY` when non-empty. This
is the one new backend code piece that makes every `subprocess.run` (conda,
pip, R) and every `urllib` call honor the proxy with no further changes.
**Bwrap passthrough already exists** (`command_worker.py:560-570`), so sandboxed
runs inherit it too.

**D3. Deploy whitelist.** Add the proxy + mirror fields to both whitelists,
mirroring `BLUEPRINT_CRAN_MIRROR`:
- `scripts/deploy_user_systemd.sh:653-691`
- `scripts/deploy_release.sh:526-575`
- `manager-agent.env` (`deploy_user_systemd.sh:699`, `deploy_release.sh:637`)
  for the Tavily case.

**D4. Reference-source mirrors live on the card, not in a separate registry.**
The earlier draft proposed a `mirror:<name>/<path>` indirection with a central
`deploy/runtime/reference-mirrors/*.json` registry. On review that couples cards
to deployment-specific mirror names (a maintenance problem: who owns the
registry? what happens when GENCODE changes its URL structure?) while saving
little — the card already needs the path.

v1 simplification: **no central registry, no `mirror:` scheme.** Geo-swapping is
handled by the `source.mirrors` list directly on the card:

```python
source = ReferenceDataSourceSpec(
    url="https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_44/gencode.v44.annotation.gtf.gz",
    mirrors=["https://mirrors.tuna.tsinghua.edu.cn/gencode/Gencode_human/release_44/gencode.v44.annotation.gtf.gz"],
    sha256="...",
    kind="gtf",
)
```

- A deployment in China can layer geo-preferred mirrors via the curated catalog
  (Layer E) or by editing the card's `source.mirrors` — no separate config file.
- The `deploy/runtime/reference-mirrors/` directory is **dropped from v1**. If a
  future need for cross-card mirror reuse appears, add a registry then; do not
  speculatively build the indirection.
- **Maintenance owner:** the curated catalog (Layer E) is maintained in-repo by
  the card-library authors, same as the bundled runtime env specs
  (`deploy/runtime/*.yml`). URL-structure changes to upstream sources
  (GENCODE reorganizing releases, Ensembl version bumps) are handled by
  catalog updates in a normal release, gated by the pinned `sha256` so a
  silently-changed file is detected rather than silently served.

**D5. manager-agent Tavily proxy.** Node `fetch` ignores proxy env. In
`manager-agent/src/server.js`, read `MANAGER_HTTP_PROXY`/`MANAGER_HTTPS_PROXY`
and, when set, install an `undici.setGlobalDispatcher(new ProxyAgent(...))` at
startup. Only affects the Tavily websearch path. (Optional; gated behind
websearch being enabled.)

**Non-goals for Layer D:** SOCKS5 proxy (HTTP/HTTPS only for v1); per-job proxy
override (deployment-global only).

---

### Layer E — Reference-source curation (data, not code)

**Goal:** ship a starter catalog so common cards "just work."

- A bundled `reference_assets` catalog for the most stable, no-auth sources:
  - GENCODE human/mouse GTF (latest + one pinned release)
  - Ensembl Biomart annotation exports
  - Common annotation DB tarballs that Bioconductor/conda don't reliably
    pre-cache (the gap noted in `docs/60_bundled_mamba_r_runtime_plan.md:316`)
- Each entry pins `sha256` so downloads are verifiable and cacheable.
- These ship as blueprint `reference_assets` defaults in the card library, not
  as pre-downloaded installer payload (see Decision: pre-bundle vs. first-run).

**Non-goals for Layer E:** TCGA/GDC bulk data (token-gated, deferred).

---

### Layer F — Dependency-resolver UX upgrade (the "spinner → progress" fix)

**Goal:** replace the indeterminate spinner with informative progress. Split
into two shippable stages so value lands early.

#### F1. Stage 1 — phase stepper + elapsed time (frontend-only, existing data)

Feasible **today** with no backend change. The `phase` enum is already
delivered over SSE and stored in `DependencyJobChipState.phase`; it is simply
not rendered. `started_at` is available via the REST poll.

- The phase enum has **8 raw values** but only **4 logical steps** plus
  terminal. The first four (`queued`, `waiting`, `launching`, `running`) are
  legacy aliases that map onto the newer, more specific four
  (`waiting_for_runtime_lock`, `building_command`, `launching_subprocess`,
  `running_subprocess`). Collapse them in the UI with an explicit alias map:

  | raw phase | displayed step | label |
  |---|---|---|
  | `queued` | 1/5 | 已排队 |
  | `waiting`, `waiting_for_runtime_lock` | 2/5 | 等待环境锁 |
  | `building_command` | 3/5 | 构建命令 |
  | `launching`, `launching_subprocess` | 4/5 | 启动子进程 |
  | `running`, `running_subprocess` | 5/5 | 执行中 |
  | `succeeded` / `failed` | terminal | done |

  (5 active steps + terminal; the `/5` denominator counts active steps so the
  bar reads honestly. `running_subprocess` is where most time is spent, so it
  is the step that most needs the elapsed timer.)

- Render as a compact stepper or `Step 3/5 · 构建命令` next to the spinner,
  not a fake determinate bar.
- Show elapsed time from `started_at` ("已运行 42s") — uses existing data,
  reduces "is it stuck?" anxiety directly.
- Keep the spinner as the indeterminate element *within* the current step.
- Single source of truth for the phase enum + alias map: extract to
  `frontend/lib/` (e.g. `dependencyPhases.ts`) and import in the three places
  that currently duplicate the raw enum (`DependencyJobChip.tsx:33-41`,
  `workspace-ui-store.ts:398-405`, `ProjectWorkspace.tsx:388-392`).

Scope: pure frontend, no schema regen, no backend deploy.

#### F2. Stage 2 — real progress: package count + bytes + speed (needs backend)

Requires the Popen migration flagged at `manager_blueprint_tools.py:1965-1967`.

- **New job fields** (`runtime_dependency_job_service.py:25-50`): `progress`
  (0-100 int), `progress_label` (e.g. `"package 3/10"` or
  `"Downloading gencode.v44.annotation.gtf"`), `bytes_total`,
  `bytes_downloaded`, `download_rate_bps`. Repurpose the dead
  `last_stdout_at`/`last_stderr_at` placeholders (L47-48) rather than adding
  more timestamp fields.
- **Persist + round-trip** in the same dict (L561-601 persist, L475-498 load).
- **Switch `_run_dependency_command` to `Popen` + line loop**
  (`manager_blueprint_tools.py:1978-1986`): `Popen(command, stdout=PIPE,
  stderr=PIPE, text=True, bufsize=1)`, then `for line in proc.stdout:`. Because
  all installers funnel here, one change covers conda/pip/CRAN/Bioconductor.
  In the loop:
  1. Append to a rolling tail (reuse `_tail_text` semantics, L3199-3203) and
     populate `job.stdout_tail`/`stderr_tail` **live** (they are currently
     terminal-only).
  2. Parse progress tokens per installer:
     - pip: `Downloading X (NN%|…| … kB/s)`.
     - conda/mamba: `Downloading …` and transaction `NN%`.
     - reference downloads (Layer B): byte counters from the `urllib` read loop.
  3. **Throttle** `phase_callback` updates (e.g. every 500 ms or on each
     integer-percent change). The callback does a full atomic-write JSON + SSE
     emit per call — unthrottled it would be too expensive at line rate.
- **Split the two back-to-back phase callbacks** (L1974-1976): emit
  `launching_subprocess` before `Popen`, `running_subprocess` after the first
  line read, so "about to launch" vs "running" become distinguishable.
- **SSE payload** (`_emit_project_event`, L380-450): forward the new fields on
  mid-run events, not just terminal. Today mid-run events carry phase only.
- **Frontend**: render a determinate bar when `progress` is present, show
  `progress_label` + `download_rate_bps` formatted ("3.2 MB/s"), and a live
  last-log-line. Fall back to the Stage-1 stepper when `progress` is absent
  (old jobs, or phases without byte metering).

Scope: backend fields + schema regen + Popen migration + SSE plumbing +
frontend bar. Larger, but Stage 1 already shipped value.

#### F3. Watchdog heartbeat

The 30 s watchdog (`runtime_dependency_job_service.py:628-712`) currently only
reconciles terminal/future state. Add a no-op heartbeat bump of
`last_heartbeat_at` if the read loop stalls (no stdout for N seconds), so the
frontend can distinguish "slow but alive" from "dead." Low effort, high
perceived-reliability payoff.

## Decisions

> The four v1 scope questions (D-3 proxy scope, D-4 pre-bundle, D-5 TCGA, GC
> policy) were explicitly confirmed with the user; all chose the minimal
> scope. D-1/D-2 are architectural defaults derived from the codebase.

### D-1. Reuse the runtime-dependency job system for reference downloads (vs. new)

**Decision: reuse.** The alternative (a parallel `reference_download_job_service`)
would duplicate per-asset locking, in-flight dedup, terminal cooling,
persistence, watchdog reconciliation, SSE channel, and a second frontend chip.
All of those already exist for package installs. A reference download is
semantically "a dependency job whose installer is a file fetch," dispatched to
its own handler (C2) rather than entangled with the package-install path.

### D-2. Download location is environment-level, not run-level

**Decision: `{data_root}/_system/reference-data/`.** This is what makes "download
once, reuse" true. A GTF fetched for card A is instantly available to card B
via sha256 dedup. Run-local `run_dir/cache` is intentionally ephemeral and
would force re-download every run — the exact problem we are solving.

### D-3. Proxy scope: deployment-global only

**Decision: deployment-global, no per-source override.** A single
`BLUEPRINT_HTTP_PROXY` / `BLUEPRINT_HTTPS_PROXY` / `BLUEPRINT_NO_PROXY` in
`backend.env` applies to all egress (package installs + reference downloads +
LLM + Tavily). No per-source or per-card proxy override in v1 — keeps the model
simple and covers the two real scenarios (corporate egress proxy; China mirror
acceleration via mirror preset). `ReferenceDataSourceSpec` therefore has no
proxy field. Proxy and mirror preset remain independently composable: a China
user can have TUNA mirrors with no proxy; a corporate user can have official
mirrors through a corporate proxy.

### D-4. Pre-bundle references in the installer vs. first-run download

**Decision: first-run download only (confirmed by user). No pre-bundle flag.**
Rationale:
- Reference files are large (GENCODE GTF ~50 MB compressed; multi-GB datasets);
  pre-bundling bloats the self-extracting installer.
- The release-bundle offline-cache pattern (`build_release_bundle.sh:340-384`)
  exists for conda packages but `docs/60:316` already notes annotation-DB
  tarballs "can't be reliably pre-downloaded" — references are worse.
- First-run download + sha256 cache gives identical reuse without installer
  bloat, and naturally degrades to "already present, skip" on upgrade.
- No `BLUEPRINT_PREBUNDLE_REFERENCES` flag in v1. If offline-first deployments
  need it later, add it as a follow-up mirroring
  `BLUEPRINT_INSTALL_PYTHON_RUNTIME=1`.

### D-5. TCGA / GDC scope

**Decision: defer (confirmed by user).** GDC requires auth tokens, has
bulk-transfer tooling (`gdc-client`), and its datasets are user-specific rather
than shared references. v1 ships no-auth mirrors only. The model deliberately
ships **no** `headers`/auth field — when TCGA enters scope, add both the field
and the token-storage decision then; do not carry a dead hook through v1.

### D-6. Reference-data GC

**Decision: manual delete only (confirmed by user).** Reuse the existing
`DELETE /api/reference-data/{ref_id}` endpoint. No LRU/automatic eviction in v1.
If `data_root` growth becomes a support issue, revisit with an LRU policy keyed
on `use_count`/`added_at` with protection for in-use items.

### D-7. Progress in two stages

**Decision: Stage 1 (frontend-only) ships first, Stage 2 (backend metering)
follows.** Stage 1 needs no backend deploy and immediately reduces anxiety.
Stage 2 is where the real bar/speed lives but requires the Popen migration and
schema regen. Staging de-risks and lets users see improvement sooner.

## Open Questions

All four v1 scope questions were confirmed with the user and are now Decisions
(D-1 through D-7). Remaining genuinely open items, none of which block v1:

1. **`pypi_mirror` is currently unwired.** The proxy work surfaces this: if we
   are touching the network layer, should v1 also wire `pypi_mirror` into pip
   (`--index-url`)? Out of scope here but worth a follow-up doc.
2. **GDC token storage location** (when TCGA enters scope in a later layer):
   global `Settings` vs per-project. Defer until then; the v1 model ships no
   token field.

## Implementation Order

1. **Layer A** (model + schema regen) — unblocks everything; zero runtime risk.
2. **Layer F1** (frontend phase stepper) — ships user-visible value first with
   **no backend deploy**. Pure frontend, no schema regen, no service restart.
   This is why F1 moves ahead of D: it is the lowest-risk, highest-immediate-
   value item, and it directly addresses the stated "转圈圈" complaint.
3. **Layer D1-D3** (Settings + startup re-export + deploy whitelist) — proxy
   plumbing lands independently and benefits package installs immediately.
   Ranked after F1 because it requires a deploy/restart, whereas F1 does not;
   the two are otherwise independent and could be reordered if a deploy window
   is already open.
4. **Layer B** (`fetch_and_register`) — the core new capability, unit-testable
   in isolation with a local file server.
5. **Layer C** (resolver/job wiring + separate handler + concurrency (C5) +
   cancel (C6) + gating + sandbox exposure) — turns B into an end-to-end
   feature. Largest single layer; split C5/C6 into follow-on PRs if it gets
   too big.
6. **Layer E** (curated catalog) — data work, no code risk, can land any time
   after B.
7. **Layer F2** (Popen + byte metering) — largest change; land last so the rest
   is stable. Benefits both package installs and reference downloads.

## Verification Plan

Per AGENTS.md "Review And Verification" — behavior-based, against real files.

### Verification status (measured after implementation)

Recorded so the "what passed" question does not have to be re-derived:

- **Backend tests:** `PYTHONPATH=backend .venv/backend/bin/python -m unittest
  discover -s backend/tests` → `Ran 515 tests`, **`FAILED (failures=1)`**.
  - The single failure,
    `test_executor_profiles.TestExecutorProfileResolution.test_clearing_default_provider_key_clears_legacy_secret`,
    **pre-exists this change**: it fails identically on clean HEAD
    (`bf9fc85`) and the implementation does not touch
    `test_executor_profiles.py` nor any provider-key field in `config.py`
    (the only `config.py` change adds proxy fields).
  - **All 514 other tests pass**, including every new test this plan
    introduced. The implementation is therefore green; "515 OK" reported in
    some summaries is inaccurate and should read "514/515, 1 pre-existing
    failure unrelated to this work."
- **Frontend build:** `cd frontend && npm run build` → passes.
- **Syntax checks:** Python (`py_compile` on all changed/new modules) and
  Node (`node --check manager-agent/src/server.js`) both pass.
- **Schema regen:** `scripts/generate_backend_schemas.py` ran clean; the
  changed models are outside the generated set, so no new schema files.

### Behavior assertions

- **Layer B core test:** spin a local HTTP server in a temp `HOME` serving a
  known file; assert `fetch_and_register` downloads, verifies sha256, dedups
  on second call (cache hit), and rejects a tampered file (sha256 mismatch).
- **Layer B retry budget (B1-retry):** mirror failover — first URL 500s,
  second succeeds. Assert transport failure (500) consumes a retry slot and
  advances to the next URL; assert sha256 mismatch advances to the next URL
  *without* consuming a retry slot. Assert the total attempt count never
  exceeds `reference_download_retries` across the whole URL list.
- **Layer B disk pre-check:** set `source.size_hint` larger than
  `shutil.disk_usage(temp_root).free`; assert `fetch_and_register` fails fast
  with `insufficient_disk_space` and performs no network I/O. Assert absence
  of `size_hint` skips the check but still cleans up on `ENOSPC` mid-stream.
- **Layer C end-to-end:** a blueprint with a `source`-only `reference_assets`
  entry, instantiated against an empty registry, triggers a background job; the
  run pauses (gating), the job downloads, and the run resumes with
  `BLUEPRINT_REFERENCE_PATHS` pointing at the registered file. Replay: second
  instantiation is a cache hit, no job.
- **Layer C concurrency (C5):** submit 2 package installs + 2 distinct
  reference downloads for the same project; assert at most 2 installs + 1
  download run concurrently (the 4th job waits on its semaphore). Assert a
  download never blocks both install slots.
- **Layer C cancel (C6):** start a slow download (throttled local server),
  call `POST .../cancel`; assert the socket aborts promptly, the partial temp
  is deleted, the job is marked `failed` with reason `cancelled`, and a re-
  submit is *not* cooled (retryable).
- **Layer D proxy test:** with `BLUEPRINT_HTTPS_PROXY` set to a local
  recording proxy, assert a pip install and a reference download both egress
  through it; assert `NO_PROXY` excludes loopback.
- **Layer D deploy test (bash):** extend `scripts/test_provisioning.sh` to
  assert proxy fields land in `backend.env` for both `deploy_user_systemd.sh`
  and `deploy_release.sh`, using a temp `HOME`. (No reference-mirror field to
  assert — D4 dropped the central registry.)
- **Layer F1:** the phase label tracks SSE phase transitions; both legacy
  (`waiting`/`launching`/`running`) and canonical phases render to the correct
  step; elapsed time ticks; spinner still present within a step.
- **Layer F2:** with a pip install producing `Downloading … kB/s` lines, assert
  `progress`/`bytes_downloaded`/`download_rate_bps` advance over SSE at ≤2 Hz
  (throttle) and the frontend bar reflects them; assert the throttle prevents
  per-line persistence storms.

## File Change Map

| Layer | File | Change |
|---|---|---|
| A | `backend/app/models/card_blueprint.py` | `ReferenceDataSourceSpec`; extend `ReferenceAssetRef` |
| A | `backend/app/schemas/*.json` | regenerate |
| A | `backend/app/services/card_library_service.py` | fix `_build_blueprint_from_card` round-trip |
| B | `backend/app/services/reference_data_service.py` | `fetch_and_register` (disk pre-check, stream, sha256, ingest) |
| C | `backend/app/services/card_library_service.py` | resolve-loop source branch (L1099) |
| C | `backend/app/services/manager_blueprint_tools.py` | new `_download_reference_asset_sync` handler + `submit(handler=…)` dispatch (L1758) |
| C | `backend/app/services/runtime_dependency_resolver_service.py` | plan-only `resolve_runtime_dependencies` reports reference-download descriptors (so Manager's "what would happen?" is accurate) |
| C | `backend/app/services/runtime_dependency_state_service.py` | dedup key includes `source.sha256` for ref jobs |
| C | `backend/app/services/runtime_dependency_job_service.py` | two kind-semaphores (C5); `cancel(job_id)` + cancel-handle registry (C6) |
| C | `backend/app/api/manager_tools.py` | `POST .../runtime-dependencies/jobs/{job_id}/cancel` (C6) |
| C | `backend/app/services/worker_service.py` | `runtime_dependency_blocker` covers ref jobs (L293) |
| C | `backend/app/workers/command_worker.py` | explicit `readonly_binds` + sandbox plan (L447, L631) |
| D | `backend/app/core/config.py` | proxy + timeout + retry fields (no mirror-registry field) |
| D | `backend/app/main.py` (or `app/core/proxy.py`) | startup re-export to `os.environ` |
| D | `scripts/deploy_user_systemd.sh`, `scripts/deploy_release.sh` | whitelist proxy fields |
| D | `manager-agent/src/server.js` | optional `undici.ProxyAgent` for Tavily |
| E | card library defaults | curated `reference_assets` catalog (mirrors inline on each source) |
| F1 | `frontend/components/dependency/DependencyJobChip.tsx` | render phase stepper + elapsed |
| F1 | `frontend/lib/dependencyPhases.ts` (new) | single phase-enum + alias-map source |
| F1 | `workspace-ui-store.ts`, `ProjectWorkspace.tsx` | import shared enum |
| F2 | `backend/app/services/runtime_dependency_job_service.py` | new fields + persist/load + callback kwargs + SSE payload |
| F2 | `backend/app/services/manager_blueprint_tools.py` | `Popen` + line loop + token parse + throttle (L1978) |
| F2 | `frontend/components/dependency/DependencyJobChip.tsx` | determinate bar + rate + live log line |

## Out Of Scope (v1)

- TCGA / GDC token-gated bulk download (D-5); no auth/token field shipped.
- SOCKS5 proxy (HTTP/HTTPS only).
- Per-source or per-card proxy override — deployment-global only (D-3).
- Reference-data automatic GC / LRU — manual delete only for v1 (D-6).
- Wiring `pypi_mirror` into pip `--index-url` (follow-up doc; Open Question 1).
- Pre-bundling references in the installer, including the
  `BLUEPRINT_PREBUNDLE_REFERENCES` flag (D-4).
- R-side reference fetching (R cards download via the executor, which is out
  of contract; if needed, route through the same job pipeline as a follow-up).

## Known Limitations (v1)

Gaps acknowledged during the implementation review. None block v1; all are
candidates for follow-up.

1. **`no_proxy` is exact-host-match only**
   (`reference_data_service.py` `_build_opener`). `no_proxy_hosts` is a set of
   literal hostnames compared with `host not in no_proxy_hosts`. It does **not**
   support standard `NO_PROXY` suffix semantics:
   - `NO_PROXY=.internal.corp.com` does **not** match `host.internal.corp.com`.
   - `NO_PROXY=*` does **not** mean "bypass proxy for everything."
   - Loopback works when listed exactly (e.g. `NO_PROXY=127.0.0.1,localhost`),
     and this is covered by `test_fetch_and_register_no_proxy_honored`.

   Subprocess installers (conda/pip/R) read `NO_PROXY` from `os.environ` and do
   their own (usually suffix-aware) matching, so this gap only affects the
   `urllib`-based reference-download path. Fix: support leading-dot suffix
   matching and a `*` wildcard in `_build_opener`. Low effort; deferred.

2. **manager-agent `NO_PROXY` not enforced per-host.** `initGlobalProxyDispatcher`
   installs an undici global dispatcher that proxies *all* Tavily egress when
   `MANAGER_HTTPS_PROXY`/`MANAGER_HTTP_PROXY` is set; `MANAGER_NO_PROXY` is
   logged but not consulted. Only affects the (opt-in) Tavily websearch path.
   Fix: use undici's `Agent` with an `EnvHttpProxyAgent` or a per-request
   proxy decision.

3. **Frontend progress falls back to the stepper on SSE loss.** The 5 s REST
   poll in `DependencyJobChip.tsx` only acts on terminal jobs; if an SSE
   event carrying `progress`/`bytes_downloaded`/`download_rate_bps` is
   dropped mid-run, the determinate bar reverts to the indeterminate F1
   stepper until the next SSE event arrives. Real progress is not lost on the
   backend; only the live display degrades.

4. **`_build_blueprint_from_card` round-trip is path-based.** It recovers
   `reference_assets` by reverse-mapping resolved `reference_paths` back to
   `ref_id` via the registry index. A source-only reference that has **not yet
   been downloaded** has no resolved path and therefore is not carried into
   the generalized blueprint — its `source` spec is dropped. This is an edge
   case (generalizing a card before its first run), not a correctness bug for
   already-materialized references.

5. **`configure_os_proxy` does not clear stale env on empty setting.** When a
   proxy field is empty, the re-export `continue`s without `os.environ.pop`-
   ing a value a previous process lifetime may have left. In practice the
   backend process starts with a clean env per systemd unit, so this is a
   theoretical concern only for long-lived dev processes that reload settings.
