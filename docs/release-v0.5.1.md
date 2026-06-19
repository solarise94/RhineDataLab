# Blueprint RE v0.5.1

Release date: 2026-06-19

Related:

- `docs/63_reference_data_pre_download_plan.md` (the plan this release ships)

## Summary

`v0.5.1` is a feature release on top of `v0.5.0` that moves large reference
databases (GTF / GFF / GENCODE / Ensembl annotations) out of the executor and
into the environment resolver, and upgrades the dependency-install UI from an
indeterminate spinner to informative progress.

Today the executor is forbidden from fetching files and can only `fail` on a
missing reference; users perceive long package installs as "stuck" because the
chip shows a static spinner that discards the phase data the backend already
sends. This release addresses both by reusing the existing runtime-dependency
resolver → job → SSE pipeline rather than building parallel systems.

## What Improved

- **Reference-data pre-download (Layers A–C of doc 63).** Blueprint
  `ReferenceAssetRef` now accepts a `source` spec (`url` + `mirrors` +
  required `sha256`) so cards can declare fetchable references. A new
  `ReferenceDataService.fetch_and_register` streams the file, verifies sha256
  incrementally, dedups against the content-addressed registry before any
  network I/O, and runs as a background dependency job — sharing the existing
  locking, dedup, watchdog, persistence, and SSE channel. Downloads land under
  `{data_root}/_system/reference-data/` (environment-level, cross-run reuse),
  not the ephemeral per-run cache.
- **Retry budget and disk safety.** Per-attempt timeout; `retries` is the total
  attempt count across the whole mirror list (not per-mirror); a sha256
  mismatch advances to the next mirror without consuming a retry slot. A disk
  pre-check fails fast on `insufficient_disk_space` when `size_hint` is set,
  instead of downloading for minutes and hitting `ENOSPC`.
- **Concurrency and cancellation.** Two kind-semaphores over the shared job
  pool (≤2 installs + ≤1 download) so a large download never starves package
  installs. A best-effort `cancel(job_id)` aborts the socket, deletes the
  partial temp, and marks the job retryable (not cooled). A race between
  `cancel()` and `_run()` that could silently drop a cancellation is fixed via
  a shared `threading.Event`.
- **Egress proxy (Layer D).** New `BLUEPRINT_HTTP_PROXY` / `HTTPS_PROXY` /
  `NO_PROXY` settings, re-exported to `os.environ` at startup so pip, conda,
  mamba, R, and `urllib` downloads all honor the proxy automatically. Surfaced
  through both deploy whitelists; manager-agent wires an undici `ProxyAgent`
  for Tavily websearch.
- **Dependency-install progress UX (Layers F1/F2).** The indeterminate spinner
  is replaced by a phase stepper ("步骤 3/5 · 构建命令") + elapsed timer using
  data the backend already sends. Stage 2 adds real progress metering:
  `Popen` + line loop parses pip/conda/download tokens, throttled to ≤2 Hz,
  and the chip renders a determinate bar with download rate ("3.2 MB/s") and a
  live log line.
- **Demo project refresh.** The `demo-rnaseq` seed now carries a
  reference-aware executor context on the DE analysis card, showcasing how a
  pre-resolved reference file is exposed to runs via `BLUEPRINT_REFERENCE_PATHS`.

## Scope Of This Release

Primary changes are concentrated in:

- `backend/app/models/card_blueprint.py` — `ReferenceDataSourceSpec`, extended
  `ReferenceAssetRef`.
- `backend/app/services/reference_data_service.py` — `fetch_and_register`.
- `backend/app/services/runtime_dependency_job_service.py` — semaphores,
  cancel, progress fields.
- `backend/app/services/manager_blueprint_tools.py` — reference-download
  handler, `Popen` metering.
- `backend/app/core/{config,proxy}.py` — proxy settings + `os.environ` re-export.
- `scripts/deploy_{user_systemd,release}.sh` — proxy whitelist.
- `frontend/components/dependency/DependencyJobChip.tsx`,
  `frontend/lib/dependencyPhases.ts` — progress UI.
- `manager-agent/src/server.js` — Tavily `ProxyAgent`.

This is a feature release (new model fields, new API endpoint, new service
capability), not a stabilization patch.

## Verification

- Backend: `PYTHONPATH=backend .venv/backend/bin/python -m unittest discover
  -s backend/tests` — 514/515 pass; the single failure
  (`test_clearing_default_provider_key_clears_legacy_secret`) pre-exists on
  clean `v0.5.0` and is unrelated to this release.
- Frontend: `cd frontend && npm run build` — passes.
- Syntax: Python `py_compile` and `node --check` both pass.
- Local services restarted successfully.

Known v1 limitations are documented in
`docs/63_reference_data_pre_download_plan.md` ("Known Limitations"), including
`no_proxy` exact-host matching and manager-agent per-host `NO_PROXY` exclusion.

## Release Positioning

Recommended release message:

> Blueprint RE v0.5.1 adds reference-data pre-download (GTF/GENCODE/Ensembl)
> as part of the environment resolver, with mirror failover, sha256
> verification, disk pre-check, and best-effort cancellation; adds
> deployment-global egress proxy support; and replaces the dependency-install
> spinner with a phase stepper, elapsed timer, and real progress bar with
> download rate.
