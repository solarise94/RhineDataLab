# 61. Dependency Install Persistence And Frontend Performance Review

Status: review + remediation plan.

Date: 2026-06-16

Related:

- `docs/44_runtime_dependency_install_visibility_and_liveness_plan.md`
- `docs/46_runtime_dependency_terminal_receipt_contract.md`
- `docs/47_oaa2_dependency_terminal_and_card_scroll_remediation.md`
- `docs/60_bundled_mamba_r_runtime_plan.md`

## Summary

This document records the findings of a two-pass review of the runtime
dependency install subsystem and the surrounding frontend, triggered by the
`oaa-2` project failing to show install progress and by reports of chat scroll
jitter and CPU spikes when a dependency task is submitted.

The review uncovered four distinct defect families:

1. **Backend persistence bug (shipped + verified fix):** a successful conda
   dependency install produced a result dict containing a non-JSON-serializable
   `pathlib.Path`, which made the dependency job state impossible to persist.
   The job stayed `running` on disk forever while the in-memory state was
   `succeeded`; the frontend therefore never received a terminal event and the
   workboard never cleared. This is fixed and verified on the live backend.
2. **Frontend CPU spikes:** a full-message `JSON.stringify` signature is
   computed on every render, the whole chat tree is reconciled without
   per-message memoization, and an unguarded thinking-panel scroll effect walks
   every message. These compound into high CPU during SSE bursts.
3. **Chat scroll jitter:** a document-flow notice toast reflows the chat column,
   a dependency-chip polling effect forms a self-sustaining re-render loop, and
   the stick-to-bottom guard is momentarily defeated by that reflow.
4. **Dependency resolve phase UX gap:** the `resolve_runtime_dependencies` phase
   (conda repoquery probe, 10-60s) emits zero events and renders only a static
   divider with no elapsed timer, so the UI appears frozen.

This document is the durable record of the review. Sections 1-4 are the
findings; Section 5 is the consolidated remediation plan.

## 1. Backend Persistence Bug — RESOLVED

### 1.1 Symptom

In the live backend journal (0.4.5, `oaa-2`), every watchdog cycle (30s) and
every frontend poll of the dependency job emitted:

```text
Terminal drift detected: project=oaa-2 job=depjob_203fac224a894cb6835dd3c97023a145
  memory_status=succeeded disk_status=stale — overwriting persisted record
Failed to self-heal persisted dependency job ...
  TypeError: Object of type PosixPath is not JSON serializable
  (app/services/utils.py:20 atomic_write_json
   -> runtime_dependency_job_service.py:557 _persist_project_jobs_locked)
```

The consequence, as observed by the user: after "Started background dependency
installation for R_env (1 package)" the frontend never showed the "正在安装"
chip and never reached a terminal state.

### 1.2 Root cause

`ManagerBlueprintTools._resolve_runtime_and_solver`
(`backend/app/services/manager_blueprint_tools.py:2822-2833`) returns a tuple of
`Path` objects: `(env_path: Path, conda_bin: Path)`.

The `_install_from_plan` **conda branch** (the only branch that exercised this
path for R/conda installs) passed the `Path` straight through to
`_run_dependency_command` without coercing to `str`:

```python
# before fix — manager_blueprint_tools.py:1673/1690
resolved_runtime, conda_bin = self._resolve_runtime_and_solver(...)
...
return self._run_dependency_command(..., resolved_runtime=resolved_runtime or "", ...)
```

`_run_dependency_command` then placed it into the result dict unconverted
(`:1822`):

```python
"resolved_runtime": resolved_runtime,   # a Path
```

`RuntimeDependencyJobService._run` assigns that dict to `job.result` and calls
`_persist_project_jobs_locked` -> `atomic_write_json` -> `json.dump`, which
raises `TypeError` on the `PosixPath`.

The `pip` and `cran`/`bioconductor` paths were already correct
(`str(env_path)`); only the conda-plan branch was missing the coercion. This is
why R packages installed via `mamba install` (the common case) triggered it
while pip/CRAN installs did not.

### 1.3 Impact chain

1. Install succeeds in memory (`job.status = "succeeded"`).
2. Persist raises `TypeError`; the caught branch logs and skips the terminal
   publish ("terminal receipt skipped to avoid chat/disk split").
3. On-disk record stays `running`.
4. Watchdog and frontend poll repeatedly detect "terminal drift", retry the
   same persist, and fail identically every 30s.
5. The `succeeded` SSE event + chat receipt are never emitted; the
   `DependencyJobChip` never renders.
6. On backend restart, `_load_project_jobs_locked` sees the stale `running`
   record and marks it `failed`/`interrupted by backend restart` — masking the
   real (successful) outcome. All 5 historical `oaa-2` jobs ended in this state.

### 1.4 Fix applied

Two changes, both shipped and verified on the live 0.4.5 backend:

**1. Core fix — `manager_blueprint_tools.py` conda branch:**
```python
# after fix
env_path, conda_bin = self._resolve_runtime_and_solver(runtime, ecosystem)
resolved_runtime = str(env_path)   # coerced up-front, mirrors pip/cran paths
```

**2. Defensive hardening — `app/services/utils.py` `atomic_write_json`:**
```python
json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
```
`default=str` is a backstop so a stray `Path`/`datetime` can never again corrupt
durable job state.

**3. Regression test — `backend/tests/test_dependency_install_serialization.py`:**
4 cases asserting that a successful conda install result is JSON-serializable,
`resolved_runtime` is `str`, and `atomic_write_json` handles `Path`/`datetime`
values. All pass (full dependency suite: 85 passed).

### 1.5 Live verification

After rebuilding the wheel, reinstalling into the release env, and restarting
`blueprint-re-backend.service`:

- New process PID serialization-error count: **0** (across multiple watchdog
  cycles).
- Live resolver call for `ComplexHeatmap` (R, R_env): `status=fully_installable`
  (previously `solver_error`/blocked — see Section 4).
- `oaa-2` demo `TCGAbiolinks` install completed and surfaced normally.

The 5 historical `interrupted` jobs on disk are unrecoverable historical
artifacts of this bug; the R packages themselves were installed. To clear the
workboard, mark them resolved via `mark_job_resolved` from the UI.

## 2. Frontend CPU Spikes — PLANNED

### 2.1 Evidence

Server-side (nginx access log, last 1000 lines for `oaa-2`):

- `/chat-sessions/session_8457b928444f`: 548 hits (533 PUT upserts + GETs).
- `/work-order`: 51, `/manager-auto`: 59, `/runtime-dependency-jobs/<id>`: 40+.

The PUTs are chat-message upserts (peak ~5/s, not infinite). The CPU spike is
therefore largely **client-side compute**, not request volume.

### 2.2 Hotspot A — full-message `JSON.stringify` on every render

`ManagerChatPanel.tsx:493-522` `sessionMessagesSignature(messages)` first
projects every message into a plain object (id, role, full `content`, full
`thinking`, full `timeline` array, attachments, state, token_usage) and then
runs `JSON.stringify` over the whole projected list. For `oaa-2` that is 71
messages / 447 timeline items. It is not a raw `JSON.stringify(messages)` — it
is a projection-then-stringify — but the projection still pulls in full content
and timeline, so the cost characteristic (O(whole list) per call) is unchanged.

It is called from two effects:

- `:859-882` hydrate effect (deps `[chatSessionQuery.data, messages, sessionId]`)
  -> calls `sessionMessagesSignature(mergedMessages)` at `:868`. Both effects
  have a "signature equal -> bail out" guard, but the stringify must run first
  to produce the signature to compare, so the guard does not avoid the cost.
- `:884-909` save effect (deps `[messages, ...]`) -> calls
  `sessionMessagesSignature(messages)` at `:892`.

Every `messages` mutation (any SSE delta, any dependency chip store update that
re-renders the panel) runs the full stringify **twice**. With auto-session
`useChatSession` refetching every 4s (`:606`), the hydrate effect re-runs on a
timer, compounding the cost.

### 2.3 Hotspot B — whole chat tree reconciled without memoization

`ManagerChatPanel.tsx:2163` `displayMessages` is not memoized, and `:2323`
`displayMessages.map(...)` renders every message + its timeline items with **no
per-message `React.memo`**. Any parent re-render reconciles the entire chat DOM
subtree. This pairs with Hotspot A: each stringify cycle is followed by a full
tree diff.

### 2.4 Hotspot C — unguarded thinking-panel scroll effect

`ManagerChatPanel.tsx:1040-1052`:

```tsx
useEffect(() => {
  messages.forEach((message) => {
    if (message.role !== "manager") return;
    (message.timeline ?? [])
      .filter((item) => item.kind === "thinking" && item.content)
      .forEach((item) => {
        const element = thinkingRefs.current[item.id];
        if (element) element.scrollTop = element.scrollHeight;
      });
  });
}, [messages]);
```

No `shouldStickToBottom` guard, runs on every `messages` change, walks every
manager message and every thinking timeline item and writes DOM. During SSE
bursts this is a repeated O(all-thinking-items) DOM-write loop.

### 2.5 Compounding feedback loops

These hotspots are amplified by the two feedback loops described in Section 3
(the dependency-chip polling self-loop and the workspace-wide subscription
cascade). A dependency-task active phase emits 5-7 phase events; each triggers
the loops, each loop iteration re-renders the chat panel, each re-render runs
Hotspots A/B/C. That is the specific trigger for "CPU 狂飙 after submitting a
dependency task."

## 3. Chat Scroll Jitter — PLANNED

### 3.1 Mechanism 1 — notice toast mount/unmount triggers workspace re-render

`ProjectWorkspace.tsx:855`:

```tsx
{notice ? <div className="notice-panel notice-toast">{notice}</div> : null}
```

The toast itself is **`position: fixed`** (`frontend/app/globals.css:4105-4115`,
`right:18px; bottom:18px; z-index:60`), so it is **out of normal document flow
and does not reflow the chat column by itself.** (An earlier draft of this
review incorrectly claimed the toast was in flow and caused layout reflow; the
CSS keeps it fixed, so that claim is wrong.)

The real effect is through re-rendering, not reflow: dependency events call
`setNotice(...)` (`:325`/`:357`) unconditionally with no dedupe, and dependency
notices persist **12s** (`:545`) before being cleared to `null`. Each `notice`
state transition (notice -> text, later text -> null) is a store mutation that
re-renders `ProjectWorkspace` (subscribed to `notice` at `:142`) and its
`ManagerChatPanel` child, compounding with Mechanism 4 (§3.4). A repeated
identical notice text still re-triggers the effect because there is no dedupe.

### 3.2 Mechanism 2 (amplifier) — DependencyJobChip polling feedback loop

`DependencyJobChip.tsx:144` puts `activeEntries` in the polling-effect deps, and
`:141` calls `poll()` synchronously on every (re)subscription. Active-phase
events mutate the store -> `activeEntries` identity changes -> effect tears down
and re-runs -> immediate `poll()` -> `updateDependencyJob` -> store mutation ->
re-render. Self-sustaining for the whole install.

### 3.3 Mechanism 3 — stick-to-bottom guard defeated during re-render bursts

`ManagerChatPanel.tsx:764-770` stick effect guards on `shouldStickToBottomRef`
and has a full dep list `[sessionId, messages, error, busy, attachments,
chatSessionQuery.isLoading]` (6 entries). The guard exists; the problem is that
the scroll listener (`:248`) uses a 48px `isNearBottom` threshold, and during
the re-render bursts from Mechanisms 1/2/4 the browser can synthesize `scroll`
events (from height recalculation / virtual list churn) that momentarily read
`isNearBottom === true`, flipping `shouldStickToBottomRef` to `true`. After
that, the next `messages` change in the dep list hard-pins
`scrollTop = scrollHeight`, yanking a scrolled-up view to the bottom.

### 3.4 Mechanism 4 — workspace-wide subscription cascade

`ProjectWorkspace.tsx:129-165` makes **34** `useWorkspaceUiStore` calls: **17
data selectors** of the form `(s) => s.xxxByProject[projectId]` (lines 129-162,
including `dependencyJobs` at `:162`) and **17 action selectors** of the form
`(s) => s.setXxx` (lines 144-165). The action selectors return stable function
references and do **not** trigger re-renders; the **17 data selectors** do. So
any one of those 17 data slices changing — including a dependency chip update
via `dependencyJobs` — re-renders the whole workspace and its
`ManagerChatPanel` child, re-firing Hotspots A/B/C.

## 4. Dependency Resolve Phase UX Gap — PLANNED

### 4.1 Silent window

`resolve_runtime_dependencies` (`manager_blueprint_tools.py:1332-1414`) emits
**zero** project events; `ManagerBlueprintTools` never calls
`_emit_project_event` and the resolver service emits nothing. During the
10-60s conda repoquery probe there is no backend signal at all.

### 4.2 Frontend rendering of the gap

- `ManagerChatPanel.tsx:2251-2261` renders `kind === "tool"` as a **static
  label + CSS line**, with no elapsed timer and no spinner.
- Contrast `:2217-2229` `kind === "thinking"` which shows "思考中 Ns" via
  `formatElapsedTime` (`:238`). Note: this timer is **not self-driven** — there
  is no `setInterval`/`now`-state in `ManagerChatPanel.tsx` that ticks it. It
  only updates when the parent re-renders (during streaming, driven by SSE
  deltas). Outside of a streaming burst it is also static. So the gap vs. the
  tool branch is really "no timer at all" vs. "timer updates opportunistically
  during SSE bursts"; an implementer should not assume a live `setInterval`
  timer already exists to reuse.
- `DependencyJobChip.tsx:28-44` phase allowlist has no `resolving`/`probing`
  entry, and depends on a `runtime_dependency_job_changed` event that the
  resolve phase never emits.
- `manager-agent/src/server.js:3537-3548` heartbeat is suppressed during tool
  execution (tool events reset `lastEmitAt`).

### 4.3 Timeline of what the user sees

| Phase | Backend events | User sees |
|-------|---------------|-----------|
| `resolve_runtime_dependencies` (10-60s) | none | static "正在解析环境依赖", no timer |
| `install_runtime_dependencies` -> depjob | `runtime_dependency_job_changed` | "正在提交..." + chip "正在安装 X..." |
| `mamba install` (minutes) | phase events | chip persists |
| terminal | succeeded/failed + receipt | chip -> "依赖安装完成" |

The resolve phase is the silent gap.

## 5. Consolidated Remediation Plan

All findings are addressed below, grouped into two batches by priority. File
paths and line numbers reference the repository state at review time.

### Batch 1 — P0 (CPU + jitter, must ship together)

These reinforce each other and must be implemented as one batch.

| # | File | Change | Leverage |
|---|------|--------|----------|
| 1 | `manager-chat/ManagerChatPanel.tsx:493` | Replace `sessionMessagesSignature` full projection + `JSON.stringify` with a lightweight signature (e.g. `length + last id + short hash of last content`), O(1) per call. | highest (CPU) |
| 2 | `manager-chat/ManagerChatPanel.tsx:2163,2323` | `useMemo` for `displayMessages`; extract a `React.memo` message-row component keyed on `message.id` + `message.state` with shallow compare. | high (CPU) |
| 3 | `manager-chat/ManagerChatPanel.tsx:1040-1052` | Add a `shouldStickToBottom`-style guard, or track which thinking items already pinned via ref, so the effect does not walk all messages and write DOM on every `messages` change. | medium (CPU) |
| 4 | `dependency/DependencyJobChip.tsx:144,141` | Remove `activeEntries` from effect deps (read via ref); call `poll()` only on mount, not on every re-subscription. Breaks the self-loop. | high (CPU + jitter) |
| 5 | `layout/ProjectWorkspace.tsx:162` | Read `dependencyJobs` via `useWorkspaceUiStore.getState()` inside the SSE closure instead of subscribing in render, so chip updates do not re-render the whole workspace. | medium (CPU + jitter) |
| 6 | `layout/ProjectWorkspace.tsx:325,357` | Dedupe `setNotice` when the text is unchanged (the toast is already `position: fixed` and does not reflow the chat column — §3.1; the real cost is the re-render it triggers, which dedupe reduces). Lower priority than #1/#4. | medium (jitter) |
| 7 | `manager-chat/ManagerChatPanel.tsx:764-770` | Trim the stick effect deps; re-check `shouldStickToBottomRef` inside `requestAnimationFrame` after layout settles. | medium (jitter) |

### Batch 2 — P1 (resolve phase UX, independent)

| # | File | Change |
|---|------|--------|
| 8 | `manager-chat/ManagerChatPanel.tsx:2251-2261` | For `kind === "tool" && status === "running"`, render "正在解析环境依赖 Ns" + spinner. Reuse `formatElapsedTime` (`:238`) for the formatting, but note the thinking branch's timer is **not self-driven** (§4.2) — it only ticks on parent re-renders. During the silent resolve phase there are no SSE deltas to drive re-renders, so a small `setInterval` (~1s) scoped to running tool items is needed to actually advance the elapsed display while the tool call is in flight. |

Optional backend enhancement (deferred to a later phase, not in Batch 2): thread
`project_event_service` into `ManagerBlueprintTools` and emit a lightweight
`dependency_resolve_started`/`dependency_resolve_finished` event so the
`DependencyJobChip` can also cover the resolve phase. This has a larger blast
radius (constructor signature + event schema) and should be designed separately.

### 5.1 Verification plan

- **Batch 1 CPU:** after the change, observe a dependency install with the
  browser profiler / Performance tab; confirm no sustained main-thread load and
  that the nginx access log no longer shows the dependency-job poll storm.
- **Batch 1 jitter:** submit a dependency task, scroll up during the active
  phase, confirm the view does not jump (no stick-to-bottom yank during the
  re-render burst).
- **Batch 2 UX:** trigger a `resolve_runtime_dependencies` call and confirm the
  divider shows a live elapsed timer + spinner rather than a static label.
- **Regression:** the existing `test_dependency_install_serialization.py` (4
  cases) and the dependency suite (85 cases) must remain green; add a frontend
  unit test for the memoized message row if feasible.

### 5.2 Status

- Section 1 (backend persistence): **fixed and verified live** in 0.4.5.
- Sections 2-4 (frontend): **planned, not yet implemented.** Code changes are
  uncommitted as of this writing.

## Appendix — Key code locations

| Concern | Location |
|---------|----------|
| Conda install result serialization (fixed) | `backend/app/services/manager_blueprint_tools.py:1673` (conda branch of `_install_from_plan`) |
| `atomic_write_json` default=str (fixed) | `backend/app/services/utils.py:15-26` |
| Regression tests (added) | `backend/tests/test_dependency_install_serialization.py` |
| Message signature stringify | `frontend/components/manager-chat/ManagerChatPanel.tsx:493,868,892` |
| Chat tree render | `frontend/components/manager-chat/ManagerChatPanel.tsx:2163,2323` |
| Thinking scroll effect | `frontend/components/manager-chat/ManagerChatPanel.tsx:1040-1052` |
| Stick-to-bottom effect | `frontend/components/manager-chat/ManagerChatPanel.tsx:764-770` |
| Tool timeline render (static label) | `frontend/components/manager-chat/ManagerChatPanel.tsx:2251-2261` |
| Dependency chip polling loop | `frontend/components/dependency/DependencyJobChip.tsx:109-144` |
| Notice toast reflow | `frontend/components/layout/ProjectWorkspace.tsx:855,538-550` |
| Workspace store subscriptions | `frontend/components/layout/ProjectWorkspace.tsx:129-162` |
| Dependency SSE handler | `frontend/components/layout/ProjectWorkspace.tsx:297-418` |
