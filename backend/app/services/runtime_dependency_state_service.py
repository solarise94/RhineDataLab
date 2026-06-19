from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from app.services.utils import read_json, utc_now


ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES = {"queued", "launching", "running", "waiting"}
BLOCKING_RUNTIME_DEPENDENCY_JOB_STATUSES = ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES | {"failed"}

# Error codes that are deterministic and should not be retried with the same request.
NON_RETRYABLE_ERROR_CODES = {
    "package_not_found_in_conda_channels",
    "github_source_install_not_supported",
    "external_source_install_not_supported",
}

# Error codes that are retryable or require inspection before retry.
RETRYABLE_OR_INSPECT_ERROR_CODES = {
    "dependency_install_timeout",
    "dependency_install_start_failed",
    "dependency_install_compilation_failed",
    "dependency_install_failed",
}


def load_runtime_dependency_jobs(project_root: Path) -> list[dict[str, Any]]:
    items = read_json(project_root / "chat" / "runtime_dependency_jobs.json", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _tail_text_bounded(text: str | None, *, max_bytes: int = 2048, max_lines: int = 50) -> str:
    """Truncate text to at most max_bytes UTF-8 bytes and max_lines lines."""
    if text is None:
        return ""
    # Line truncation first
    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        truncated_lines = lines[-max_lines:]
        text = "".join(truncated_lines)
    # Byte truncation
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        # Truncate from the start to stay within max_bytes, preserving valid UTF-8
        truncated = encoded[-max_bytes:]
        # If we cut in the middle of a multi-byte sequence, strip incomplete prefix
        try:
            text = truncated.decode("utf-8")
        except UnicodeDecodeError:
            # Strip until we get valid UTF-8
            for i in range(1, 5):
                try:
                    text = truncated[i:].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    pass
            else:
                text = truncated.decode("utf-8", errors="replace")
    return text


def _normalize_packages_for_key(packages: list[str], ecosystem: str) -> list[str]:
    """Normalize package names for dedupe key comparison.

    - Trim whitespace.
    - Lowercase (Python packages are case-insensitive; R packages are case-sensitive
      for display but lowercased for key comparison).
    - Remove duplicates while preserving order.
    """
    seen: set[str] = set()
    normalized: list[str] = []
    for pkg in packages:
        name = str(pkg).strip()
        if not name:
            continue
        key_name = name.lower()
        if key_name not in seen:
            seen.add(key_name)
            normalized.append(key_name)
    return normalized


def compute_dedupe_key(
    ecosystem: str,
    runtime: str,
    packages: list[str],
    *,
    error_code: str | None = None,
    requested_package: str | None = None,
    source_sha256: str | None = None,
) -> str:
    """Compute a stable dedupe key for runtime dependency jobs.

    Format for package jobs:
      dep:{ecosystem}:{runtime}:{sorted_normalized_packages}:{error_code}:{requested_package}
    Format for reference-data download jobs (Layer C):
      ref:{source_sha256}:{error_code}:{requested_package}
    """
    if source_sha256:
        return f"ref:{source_sha256}:{error_code or ''}:{requested_package or ''}"
    normalized = _normalize_packages_for_key(packages, ecosystem)
    pkg_str = ",".join(sorted(normalized))
    return f"dep:{ecosystem}:{runtime}:{pkg_str}:{error_code or ''}:{requested_package or ''}"


def _retry_hint_for_error_code(error_code: str | None) -> str | None:
    """Return a deterministic retry hint for a given error code."""
    if error_code == "package_not_found_in_conda_channels":
        return "do_not_retry_same_conda_request"
    if error_code in ("github_source_install_not_supported", "external_source_install_not_supported"):
        return "do_not_retry_installer"
    if error_code == "dependency_install_timeout":
        return "retry_allowed_after_runtime_check"
    if error_code == "dependency_install_start_failed":
        return "manual_runtime_preparation_required"
    if error_code == "dependency_install_compilation_failed":
        return "manual_system_dependency_or_runtime_preparation_required"
    if error_code == "dependency_install_failed":
        return "inspect_stderr"
    return None


def runtime_dependency_failure_details(job: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Normalize a runtime dependency job (dict or dataclass) into a consistent failure-detail shape.

    This helper avoids importing RuntimeDependencyJobService to prevent circular imports.
    It accepts either a persisted job dictionary or any duck-typed object with the expected fields.
    """
    # Handle both dict-like and dataclass-like objects
    if isinstance(job, dict):
        getter = job.get
    else:
        getter = lambda key, default=None: getattr(job, key, default)  # noqa: E731

    job_id = str(getter("job_id") or "")
    task_id = str(getter("task_id") or "")
    status = str(getter("status") or "")
    payload = getter("payload") if isinstance(getter("payload"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    result = getter("result") if isinstance(getter("result"), dict) else {}
    error = str(getter("error") or "")

    runtime = str(payload.get("runtime") or result.get("runtime") or "").strip()
    resolved_runtime = str(result.get("resolved_runtime") or "").strip() or None
    ecosystem = str(payload.get("ecosystem") or result.get("ecosystem") or "").strip() or None
    manager = str(payload.get("manager") or result.get("manager") or "").strip() or None
    packages = list(payload.get("packages") or result.get("packages") or [])

    card_id = str(source.get("card_id") or "").strip() or None
    run_id = str(source.get("run_id") or "").strip() or None
    session_id = str(source.get("session_id") or "").strip() or None

    ok = bool(result.get("ok")) if result else None
    error_code = str(result.get("error_code") or "").strip() or None
    message = str(result.get("message") or error or "").strip() or None
    requested_package = str(result.get("requested_package") or "").strip() or None
    attempted_candidates = list(result.get("attempted_candidates") or []) or None
    fallback_available = list(result.get("fallback_available") or []) or None

    stdout_tail_raw = str(result.get("stdout_tail") or "")
    stderr_tail_raw = str(result.get("stderr_tail") or "")
    stdout_tail = _tail_text_bounded(stdout_tail_raw)
    stderr_tail = _tail_text_bounded(stderr_tail_raw)
    truncated = (
        len(stdout_tail_raw.encode("utf-8")) > 2048
        or len(stdout_tail_raw.splitlines()) > 50
        or len(stderr_tail_raw.encode("utf-8")) > 2048
        or len(stderr_tail_raw.splitlines()) > 50
    )

    retry_hint = _retry_hint_for_error_code(error_code)
    source_sha256 = _reference_source_sha256(payload)
    dedupe_key = compute_dedupe_key(
        ecosystem or "unknown",
        runtime or "unknown",
        packages,
        error_code=error_code,
        requested_package=requested_package,
        source_sha256=source_sha256,
    )

    created_at = str(getter("created_at") or "").strip() or None
    started_at = str(getter("started_at") or "").strip() or None
    finished_at = str(getter("finished_at") or "").strip() or None

    details: dict[str, Any] = {
        "job_id": job_id,
        "task_id": task_id,
        "status": status,
        "runtime": runtime,
        "resolved_runtime": resolved_runtime,
        "ecosystem": ecosystem,
        "manager": manager,
        "packages": packages,
        "card_id": card_id,
        "run_id": run_id,
        "session_id": session_id,
        "ok": ok,
        "error_code": error_code,
        "message": message,
        "requested_package": requested_package,
        "attempted_candidates": attempted_candidates,
        "fallback_available": fallback_available,
        "stdout_tail": stdout_tail or None,
        "stderr_tail": stderr_tail or None,
        "truncated": truncated,
        "retry_hint": retry_hint,
        "dedupe_key": dedupe_key,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }

    # Strip None values for cleaner payloads, but keep explicit False/[]/""
    return {k: v for k, v in details.items() if v is not None}


def _blocker_group_key(item: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return (card_id, ecosystem, runtime, normalized_packages) for grouping blockers."""
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    card_id = str(source.get("card_id") or "").strip()
    if not card_id:
        return None
    ecosystem = str(payload.get("ecosystem") or "").strip()
    runtime = str(payload.get("runtime") or "").strip()
    packages = payload.get("packages")
    if isinstance(packages, list):
        # Sort so that ["numpy", "pandas"] and ["pandas", "numpy"] share the same key,
        # matching compute_dedupe_key() normalization.
        normalized = ",".join(sorted(_normalize_packages_for_key(packages, ecosystem)))
    else:
        normalized = ""
    return (card_id, ecosystem, runtime, normalized)


def dependency_blockers_by_card(project_root: Path) -> dict[str, dict[str, Any]]:
    # Group by (card_id, ecosystem, runtime, normalized_packages) so that a
    # manually resolved job for one package set does not mask an unresolved
    # failure for a different package set on the same card.
    latest_by_group: dict[tuple[str, str, str, str], tuple[tuple[str, str], dict[str, Any]]] = {}
    for item in load_runtime_dependency_jobs(project_root):
        group_key = _blocker_group_key(item)
        if group_key is None:
            continue
        status = str(item.get("status") or "").strip()
        created_at = str(item.get("created_at") or item.get("started_at") or item.get("finished_at") or "")
        job_id = str(item.get("job_id") or "")
        current_key = (created_at, job_id)
        existing = latest_by_group.get(group_key)
        if existing is None or current_key >= existing[0]:
            latest_by_group[group_key] = (current_key, item)

    # Filter out groups whose latest job is manually resolved or non-blocking.
    blocking: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for group_key, (_key, item) in latest_by_group.items():
        status = str(item.get("status") or "").strip()
        if status not in BLOCKING_RUNTIME_DEPENDENCY_JOB_STATUSES:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        resolution_status = str(
            item.get("resolution_status") or payload.get("resolution_status") or ""
        ).strip()
        if resolution_status == "manually_resolved":
            continue
        blocking[group_key] = item

    # Return the latest unresolved blocker per card_id. If a card has multiple
    # unresolved groups, the latest job wins.
    latest_by_card: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
    for (card_id, _ecosystem, _runtime, _packages), item in blocking.items():
        created_at = str(item.get("created_at") or item.get("started_at") or item.get("finished_at") or "")
        job_id = str(item.get("job_id") or "")
        current_key = (created_at, job_id)
        existing = latest_by_card.get(card_id)
        if existing is None or current_key >= existing[0]:
            latest_by_card[card_id] = (current_key, item)

    blockers: dict[str, dict[str, Any]] = {}
    for card_id, (_key, item) in latest_by_card.items():
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        details = runtime_dependency_failure_details(item)
        status = str(item.get("status") or "").strip()
        blockers[card_id] = {
            "job_id": details.get("job_id", ""),
            "task_id": details.get("task_id", ""),
            "status": status,
            "runtime": details.get("runtime", ""),
            "packages": details.get("packages", []),
            "run_id": details.get("run_id") or "",
            "session_id": details.get("session_id") or "",
            "error_code": details.get("error_code"),
            "message": details.get("message"),
            "requested_package": details.get("requested_package"),
            "attempted_candidates": details.get("attempted_candidates"),
            "fallback_available": details.get("fallback_available"),
            "retry_hint": details.get("retry_hint"),
            "dedupe_key": details.get("dedupe_key"),
            "result": item.get("result") if isinstance(item.get("result"), dict) else {},
            "error": str(item.get("error") or ""),
        }
    return blockers


def dependency_blocker_for_card(project_root: Path, card_id: str) -> dict[str, Any] | None:
    if not card_id:
        return None
    return dependency_blockers_by_card(project_root).get(card_id)


def _request_key_from_payload(payload: dict[str, Any]) -> tuple[str, str, list[str]] | None:
    """Extract (ecosystem, runtime, packages) from a job payload for duplicate lookup."""
    if not isinstance(payload, dict):
        return None
    ecosystem = str(payload.get("ecosystem") or "").strip()
    runtime = str(payload.get("runtime") or "").strip()
    packages = payload.get("packages")
    if not ecosystem or not runtime or not isinstance(packages, list):
        return None
    return (ecosystem, runtime, list(packages))


def _reference_source_sha256(payload: dict[str, Any]) -> str | None:
    """Extract the source sha256 from a reference-download job payload."""
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    spec = source.get("spec")
    if not isinstance(spec, dict):
        return None
    sha256 = str(spec.get("sha256") or "").strip()
    return sha256 or None


def find_duplicate_in_flight(
    project_root: Path,
    ecosystem: str,
    runtime: str,
    packages: list[str],
    *,
    source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Return a matching in-flight job.

    For package jobs, matches ecosystem/runtime/packages.
    For reference-data download jobs (Layer C), matches source.sha256.
    Looks at persisted jobs (not just in-memory state) so backend restart does
    not reset deduplication.
    """
    if source_sha256:
        for item in load_runtime_dependency_jobs(project_root):
            status = str(item.get("status") or "").strip()
            if status not in ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            item_sha256 = _reference_source_sha256(payload)
            if item_sha256 == source_sha256:
                return {
                    "prior_job_id": str(item.get("job_id") or ""),
                    "prior_status": status,
                    "prior_phase": str(item.get("phase") or status).strip(),
                    "prior_created_at": str(item.get("created_at") or ""),
                }
        return None

    expected = _normalize_packages_for_key(packages, ecosystem)
    for item in load_runtime_dependency_jobs(project_root):
        status = str(item.get("status") or "").strip()
        if status not in ACTIVE_RUNTIME_DEPENDENCY_JOB_STATUSES:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        req = _request_key_from_payload(payload)
        if req is None:
            continue
        item_ecosystem, item_runtime, item_packages = req
        if item_ecosystem != ecosystem or item_runtime != runtime:
            continue
        item_normalized = _normalize_packages_for_key(item_packages, ecosystem)
        if item_normalized == expected:
            return {
                "prior_job_id": str(item.get("job_id") or ""),
                "prior_status": status,
                "prior_phase": str(item.get("phase") or status).strip(),
                "prior_created_at": str(item.get("created_at") or ""),
            }
    return None


def find_duplicate_terminal_failure(
    project_root: Path,
    ecosystem: str,
    runtime: str,
    packages: list[str],
    *,
    source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Return a matching terminal failed job that should not be retried.

    For package jobs, matches ecosystem/runtime/packages and only cools
    deterministic non-retryable error codes.
    For reference-data download jobs (Layer C), matches source.sha256.
    Cancelled jobs are always retryable and are never cooled.
    """
    if source_sha256:
        for item in load_runtime_dependency_jobs(project_root):
            status = str(item.get("status") or "").strip()
            if status not in {"failed", "succeeded"}:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            item_sha256 = _reference_source_sha256(payload)
            if item_sha256 != source_sha256:
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            error_code = str(result.get("error_code") or "").strip() or None
            if status == "succeeded":
                return {
                    "prior_job_id": str(item.get("job_id") or ""),
                    "prior_status": "succeeded",
                    "prior_created_at": str(item.get("created_at") or ""),
                }
            # Cancellations are retryable; do not cool.
            if status == "failed" and error_code == "cancelled":
                return None
            # Cool reference failures with non-retryable error codes only.
            if status == "failed" and error_code in NON_RETRYABLE_ERROR_CODES:
                dedupe_key = compute_dedupe_key(
                    ecosystem,
                    runtime,
                    packages,
                    error_code=error_code,
                    source_sha256=source_sha256,
                )
                return {
                    "prior_job_id": str(item.get("job_id") or ""),
                    "prior_status": "failed",
                    "prior_error_code": error_code,
                    "prior_created_at": str(item.get("created_at") or ""),
                    "dedupe_key": dedupe_key,
                }
        return None

    expected = _normalize_packages_for_key(packages, ecosystem)
    for item in load_runtime_dependency_jobs(project_root):
        status = str(item.get("status") or "").strip()
        if status not in {"failed", "succeeded"}:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        req = _request_key_from_payload(payload)
        if req is None:
            continue
        item_ecosystem, item_runtime, item_packages = req
        if item_ecosystem != ecosystem or item_runtime != runtime:
            continue
        item_normalized = _normalize_packages_for_key(item_packages, ecosystem)
        if item_normalized != expected:
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        error_code = str(result.get("error_code") or "").strip() or None
        # For succeeded jobs, we could return a positive duplicate, but the
        # primary use case is cooling failed jobs. For now, only cool failed.
        if status == "succeeded":
            return {
                "prior_job_id": str(item.get("job_id") or ""),
                "prior_status": "succeeded",
                "prior_created_at": str(item.get("created_at") or ""),
            }
        if status == "failed" and error_code in NON_RETRYABLE_ERROR_CODES:
            requested_package = str(result.get("requested_package") or "").strip() or None
            dedupe_key = compute_dedupe_key(
                ecosystem,
                runtime,
                packages,
                error_code=error_code,
                requested_package=requested_package,
            )
            return {
                "prior_job_id": str(item.get("job_id") or ""),
                "prior_status": "failed",
                "prior_error_code": error_code,
                "prior_created_at": str(item.get("created_at") or ""),
                "dedupe_key": dedupe_key,
                "fallback_available": list(result.get("fallback_available") or []) or None,
                "requested_package": requested_package,
            }
    return None
