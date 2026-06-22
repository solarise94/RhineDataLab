from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

from pydantic import ValidationError

from app.models.runs import Manifest
from app.services.artifact_format_service import detect_artifact_class, detect_artifact_format
from app.services.manifest_service import ManifestService
from app.services.utils import atomic_write_json

MAX_MANIFEST_REPAIR_ATTEMPTS = 3
TRACE_LINE_LIMIT = 20
TRACE_FILE_LIMIT = 200
STDOUT_FORWARD_CHAR_LIMIT = 4_000
SENSITIVE_TOKEN_RE = re.compile(r"(api[_-]?key|token|password|secret|sk-[A-Za-z0-9_-]+)", re.IGNORECASE)
OUTPUT_TIMELINE_NAME = "agent_output_timeline.jsonl"
OUTPUT_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "package_install",
        re.compile(
            r"install\.packages|BiocManager|R CMD INSTALL|conda install|pip install|npm install|"
            r"installing package|downloaded|trying URL|Content type|compil",
            re.IGNORECASE,
        ),
    ),
    ("tool_call", re.compile(r"tool call|function call|run_command|read_file|write_file|edit_file|apply_patch|shell", re.IGNORECASE)),
    ("code_run", re.compile(r"Rscript|python|bash|source\(|Executing|Running|script|analysis\.R|analysis\.py", re.IGNORECASE)),
    ("manifest", re.compile(r"manifest|created_assets|inputs_used|code_artifacts|schema validation|repair", re.IGNORECASE)),
    ("error", re.compile(r"error|failed|exception|traceback|timeout|no such file|not found", re.IGNORECASE)),
    ("result_summary", re.compile(r"result|output files|metric|complete|summary|significant|genes analyzed|PCA|DESeq2", re.IGNORECASE)),
    ("thinking_like", re.compile(r"\bI need\b|\bI'll\b|\bLet me\b|\bwe need\b|plan|思考|需要|计划|检查", re.IGNORECASE)),
]


def _parse_args() -> object:
    parser = ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--task-packet", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--auth-mode", default=None)
    return parser.parse_args()


def _load_task_packet(packet_path: Path) -> dict:
    return json.loads(packet_path.read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _redact_text(value: str) -> str:
    return SENSITIVE_TOKEN_RE.sub("[REDACTED]", value)


def _redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for token in command:
        lowered = token.lower()
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if any(marker in lowered for marker in ("key", "token", "password", "secret")):
            redacted.append("[REDACTED]")
            redact_next = "=" not in token
            continue
        redacted.append(_redact_text(token))
    return redacted


def _write_trace(run_dir: Path, trace: dict[str, Any]) -> None:
    trace["updated_at"] = _utc_now()
    atomic_write_json(run_dir / "agent_trace.json", trace)


def _classify_output_line(line: str) -> str:
    if line.startswith("BP_EVENT "):
        return "bp_event"
    for kind, pattern in OUTPUT_KIND_PATTERNS:
        if pattern.search(line):
            return kind
    return "plain_output"


def _append_output_timeline_event(
    run_dir: Path,
    *,
    trace_started_monotonic: float,
    phase: str,
    attempt: int,
    event_type: str,
    kind: str,
    text: str = "",
    line_number: int | None = None,
    gap_since_previous_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    redacted_text = _redact_text(text)
    event: dict[str, Any] = {
        "schema_version": "agent_output_timeline.v1",
        "timestamp": _utc_now(),
        "offset_seconds": round(max(0.0, time.monotonic() - trace_started_monotonic), 3),
        "phase": phase,
        "attempt": attempt,
        "event_type": event_type,
        "kind": kind,
        "line_number": line_number,
        "gap_since_previous_seconds": gap_since_previous_seconds,
        "text": redacted_text,
        "char_count": len(redacted_text),
        "redacted": redacted_text != text,
        "metadata": metadata or {},
    }
    with (run_dir / OUTPUT_TIMELINE_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _record_output_kind(attempt_record: dict[str, Any], kind: str, line: str) -> None:
    counts = attempt_record.setdefault("output_kind_counts", {})
    entry = counts.setdefault(kind, {"lines": 0, "chars": 0})
    entry["lines"] += 1
    entry["chars"] += len(line)


def _stdout_preview(line: str) -> tuple[str, bool]:
    if len(line) <= STDOUT_FORWARD_CHAR_LIMIT:
        return line, False
    head = line[:STDOUT_FORWARD_CHAR_LIMIT]
    return f"{head}\n[wrapper] stdout line truncated: original length {len(line)} characters. Full content should be written to result files, not stdout.", True


def _new_trace(*, provider: str, packet: dict, run_dir: Path, project_root: Path, template: str, auth_mode: str | None = None, profile_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "agent_trace.v1",
        "run_id": packet.get("task_id"),
        "project_id": packet.get("project_id"),
        "card_id": packet.get("card_id"),
        "card_title": packet.get("card_title"),
        "provider": provider,
        "auth_mode": auth_mode,
        "profile_id": profile_id,
        "started_at": _utc_now(),
        "finished_at": None,
        "total_duration_seconds": None,
        "status": "running",
        "current_phase": "initializing",
        "project_root": str(project_root),
        "run_dir": str(run_dir),
        "launch_template_preview": _redact_text(template),
        "provider_attempts": [],
        "manifest_validation": [],
        "file_timeline": [],
        "observations": [],
        "updated_at": _utc_now(),
    }


def _record_observation(trace: dict[str, Any], code: str, message: str, *, severity: str = "info") -> None:
    observations = trace.setdefault("observations", [])
    if any(item.get("code") == code and item.get("message") == message for item in observations):
        return
    observations.append({"severity": severity, "code": code, "message": message, "created_at": _utc_now()})


def _relative_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        try:
            return path.relative_to(project_root.resolve()).as_posix()
        except (OSError, ValueError):
            return str(path)


def _file_entry(project_root: Path, path: Path, *, category: str, started_monotonic: float) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        return None
    return {
        "path": _relative_path(project_root, path),
        "category": category,
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "mtime_offset_seconds": round(max(0.0, time.monotonic() - started_monotonic), 3),
    }


def _collect_file_timeline(project_root: Path, run_dir: Path, packet: dict, *, started_monotonic: float) -> list[dict[str, Any]]:
    paths: list[tuple[Path, str]] = []
    for filename in (
        "manifest.candidate.json",
        "manifest.json",
        "manifest_repair_prompt.md",
        "manager_brief.json",
        "transcript.md",
        OUTPUT_TIMELINE_NAME,
    ):
        paths.append((run_dir / filename, "run_file"))
    for item in packet.get("expected_outputs", []):
        path_hint = item.get("path_hint")
        if path_hint:
            paths.append((project_root / path_hint, f"expected_output:{item.get('role') or ''}"))
    script_dir = project_root / "scripts" / "generated" / str(packet.get("task_id", ""))
    if script_dir.exists():
        paths.extend((path, "generated_script") for path in sorted(script_dir.rglob("*")) if path.is_file())

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for path, category in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        entry = _file_entry(project_root, path, category=category, started_monotonic=started_monotonic)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= TRACE_FILE_LIMIT:
            break
    return sorted(entries, key=lambda item: (item["mtime"], item["path"]))


def _launch_template_mapping(
    *,
    provider: str,
    packet: dict,
    packet_path: Path,
    run_dir: Path,
    project_root: Path,
    prompt_path: Path | None = None,
) -> dict[str, str]:
    candidate_path = run_dir / "manifest.candidate.json"
    executor_prompt_path = prompt_path or run_dir / "executor_prompt.md"
    backend_root = Path(__file__).resolve().parents[2]
    repo_root = backend_root.parent
    return {
        "python": sys.executable,
        "project_root": str(project_root),
        "run_dir": str(run_dir),
        "result_dir": str(project_root / packet["run_context"]["result_dir"]),
        "task_packet_path": str(packet_path),
        "manifest_path": str(candidate_path),
        "manifest_candidate_path": str(candidate_path),
        "final_manifest_path": str(run_dir / "manifest.json"),
        "transcript_path": str(run_dir / "transcript.md"),
        "executor_brief_path": str(run_dir / "executor_brief.md"),
        "executor_prompt_path": str(executor_prompt_path),
        "adapter_contract_path": str(run_dir / "adapter_contract.json"),
        "manager_brief_path": str(run_dir / "manager_brief.json"),
        "manifest_repair_prompt_path": str(run_dir / "manifest_repair_prompt.md"),
        "worker_type": provider,
        "repo_root": str(repo_root),
    }


def _render_launch_command(
    template: str,
    *,
    provider: str,
    packet_path: Path,
    run_dir: Path,
    project_root: Path,
    prompt_path: Path | None = None,
    packet: dict | None = None,
) -> list[str]:
    mapping = _launch_template_mapping(
        provider=provider,
        packet=packet or json.loads(packet_path.read_text(encoding="utf-8")),
        packet_path=packet_path,
        run_dir=run_dir,
        project_root=project_root,
        prompt_path=prompt_path,
    )
    try:
        # Quote values to protect paths with spaces in the legacy string template
        quoted_mapping = {k: shlex.quote(v) for k, v in mapping.items()}
        return shlex.split(template.format(**quoted_mapping))
    except KeyError as exc:
        missing = exc.args[0]
        raise RuntimeError(f"Launch template for provider={provider} referenced unknown placeholder {{{missing}}}.") from exc


def _render_launch_argv_template(
    template: list[str],
    *,
    provider: str,
    packet_path: Path,
    run_dir: Path,
    project_root: Path,
    prompt_path: Path | None = None,
    packet: dict | None = None,
) -> list[str]:
    """Render a structured argv template by substituting placeholders.

    This is preferred over _render_launch_command() to avoid shlex.split issues
    with paths containing spaces.
    """
    mapping = _launch_template_mapping(
        provider=provider,
        packet=packet or json.loads(packet_path.read_text(encoding="utf-8")),
        packet_path=packet_path,
        run_dir=run_dir,
        project_root=project_root,
        prompt_path=prompt_path,
    )
    try:
        return [item.format(**mapping) for item in template]
    except KeyError as exc:
        missing = exc.args[0]
        raise RuntimeError(f"Launch argv template for provider={provider} referenced unknown placeholder {{{missing}}}.") from exc


def _run_provider_command(
    command: list[str],
    *,
    project_root: Path,
    env: dict[str, str],
    run_dir: Path,
    trace: dict[str, Any],
    phase: str,
    attempt: int,
    packet: dict,
    trace_started_monotonic: float,
    timeout: float | None = None,
) -> int:
    started = time.monotonic()
    attempt_record: dict[str, Any] = {
        "phase": phase,
        "attempt": attempt,
        "started_at": _utc_now(),
        "finished_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "command": _redact_command(command),
        "stdout_line_count": 0,
        "bp_event_count": 0,
        "output_kind_counts": {},
        "first_output_at": None,
        "last_output_at": None,
        "last_output_lines": [],
    }
    trace["current_phase"] = phase
    trace.setdefault("provider_attempts", []).append(attempt_record)
    _write_trace(run_dir, trace)
    _append_output_timeline_event(
        run_dir,
        trace_started_monotonic=trace_started_monotonic,
        phase=phase,
        attempt=attempt,
        event_type="process_start",
        kind="system_event",
        metadata={"command": _redact_command(command)},
    )
    try:
        import os
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"[wrapper] failed to launch provider command: {exc}", flush=True)
        attempt_record["finished_at"] = _utc_now()
        attempt_record["duration_seconds"] = round(time.monotonic() - started, 3)
        attempt_record["exit_code"] = 2
        attempt_record["launch_error"] = str(exc)
        trace["file_timeline"] = _collect_file_timeline(
            project_root, run_dir, packet, started_monotonic=trace_started_monotonic
        )
        _record_observation(trace, "provider_launch_failed", f"Provider command failed to launch: {exc}", severity="error")
        _write_trace(run_dir, trace)
        _append_output_timeline_event(
            run_dir,
            trace_started_monotonic=trace_started_monotonic,
            phase=phase,
            attempt=attempt,
            event_type="process_launch_error",
            kind="error",
            text=str(exc),
        )
        return 2
    last_line_monotonic = started
    
    from threading import Thread
    
    def pump_stdout():
        nonlocal last_line_monotonic
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if line:
                    forwarded_line, truncated = _stdout_preview(line)
                    print(forwarded_line, flush=True)
                    line_seen_monotonic = time.monotonic()
                    now = _utc_now()
                    attempt_record["stdout_line_count"] += 1
                    if attempt_record["first_output_at"] is None:
                        attempt_record["first_output_at"] = now
                    attempt_record["last_output_at"] = now
                    kind = _classify_output_line(line)
                    if kind == "bp_event":
                        attempt_record["bp_event_count"] += 1
                    _record_output_kind(attempt_record, kind, line)
                    _append_output_timeline_event(
                        run_dir,
                        trace_started_monotonic=trace_started_monotonic,
                        phase=phase,
                        attempt=attempt,
                        event_type="stdout_line",
                        kind=kind,
                        text=forwarded_line,
                        line_number=attempt_record["stdout_line_count"],
                        gap_since_previous_seconds=round(line_seen_monotonic - last_line_monotonic, 3),
                        metadata={"truncated": truncated, "original_char_count": len(line)} if truncated else {},
                    )
                    last_line_monotonic = line_seen_monotonic
                    lines = attempt_record["last_output_lines"]
                    lines.append(_redact_text(line)[:1000])
                    del lines[:-TRACE_LINE_LIMIT]
                    if attempt_record["stdout_line_count"] == 1 or attempt_record["stdout_line_count"] % 25 == 0:
                        trace["file_timeline"] = _collect_file_timeline(
                            project_root, run_dir, packet, started_monotonic=trace_started_monotonic
                        )
                        _write_trace(run_dir, trace)

    reader = Thread(target=pump_stdout, daemon=True)
    reader.start()
    
    timed_out = False
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        attempt_record["timed_out"] = True
        print(f"[wrapper] provider command timed out after {timeout} seconds during {phase}.", flush=True)
        try:
            pgid = os.getpgid(process.pid)
            import signal
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        return_code = process.wait()
        
    reader.join(timeout=2)
    if process.stdout is not None:
        try:
            process.stdout.close()
        except Exception:
            pass

    attempt_record["finished_at"] = _utc_now()
    attempt_record["duration_seconds"] = round(time.monotonic() - started, 3)
    attempt_record["exit_code"] = return_code
    attempt_record["final_silence_seconds"] = round(max(0.0, time.monotonic() - last_line_monotonic), 3)
    trace["file_timeline"] = _collect_file_timeline(project_root, run_dir, packet, started_monotonic=trace_started_monotonic)
    if return_code != 0:
        if timed_out:
            _record_observation(trace, "provider_timeout", f"{phase} attempt {attempt} timed out after {timeout} seconds.", severity="error")
        else:
            _record_observation(trace, "provider_nonzero_exit", f"{phase} attempt {attempt} exited with {return_code}.", severity="error")
    if attempt_record["duration_seconds"] and attempt_record["duration_seconds"] > 120 and not timed_out:
        _record_observation(trace, "slow_provider_attempt", f"{phase} attempt {attempt} took {attempt_record['duration_seconds']} seconds.", severity="warning")
    _write_trace(run_dir, trace)
    
    event_type = "process_timeout" if timed_out else "process_exit"
    _append_output_timeline_event(
        run_dir,
        trace_started_monotonic=trace_started_monotonic,
        phase=phase,
        attempt=attempt,
        event_type=event_type,
        kind="system_event",
        metadata={"exit_code": return_code, "duration_seconds": attempt_record["duration_seconds"], "timed_out": timed_out},
        gap_since_previous_seconds=attempt_record["final_silence_seconds"],
    )
    if timed_out:
        return 124
    return return_code


def _manifest_validation_errors(path: Path, *, packet: dict[str, Any] | None = None, project_root: Path | None = None) -> list[str]:
    if not path.exists():
        return [f"{path.name} is missing."]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path.name} is not valid JSON: {exc}"]
    try:
        manifest = ManifestService.normalize_manifest_payload(payload, run_id=str((packet or {}).get("task_id") or path.parent.name))
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return [str(error) for error in exc.errors()]
        return [str(exc)]
    if not packet:
        return []

    errors: list[str] = []

    expected_outputs = packet.get("expected_outputs") if isinstance(packet.get("expected_outputs"), list) else []
    expected_by_role = {
        item.get("role"): item
        for item in expected_outputs
        if isinstance(item, dict) and item.get("role") and item.get("path_hint") and item.get("artifact_class")
    }
    required_roles = {
        role
        for role, item in expected_by_role.items()
        if role and item.get("required", True) is not False
    }
    created_by_role = {item.role: item for item in manifest.created_assets if item.role}
    missing_output_roles = sorted(required_roles - set(created_by_role))
    if missing_output_roles:
        formatted = ", ".join(
            f"{role} ({expected_by_role[role]['path_hint']})" for role in missing_output_roles if role in expected_by_role
        )
        errors.append(f"{path.name} is missing created_assets for expected outputs: {formatted}")
    if project_root is not None:
        for role, asset in created_by_role.items():
            expected = expected_by_role.get(role)
            if not expected:
                continue
            output_path = project_root / asset.path
            if not output_path.exists():
                errors.append(f"Missing output file: {asset.path} ({role})")
                continue
            detected_class = detect_artifact_class(output_path)
            detected_format = detect_artifact_format(output_path)
            if detected_class != expected.get("artifact_class"):
                errors.append(
                    f"{path.name} output class mismatch for {role}: expected {expected.get('artifact_class')}, got {detected_class or 'unknown'}"
                )
            accepted_formats = list(expected.get("accepted_formats") or [])
            if accepted_formats and detected_format not in accepted_formats:
                errors.append(
                    f"{path.name} output format mismatch for {role}: expected one of {', '.join(accepted_formats)}, got {detected_format or 'unknown'}"
                )
    return errors


def _promote_candidate_manifest(*, run_dir: Path) -> list[str]:
    candidate_path = run_dir / "manifest.candidate.json"
    manifest_path = run_dir / "manifest.json"
    packet_path = run_dir / "task_packet.json"
    packet = None
    if packet_path.exists():
        try:
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            packet = None
    _sync_run_generated_scripts(run_dir=run_dir, packet=packet)
    if candidate_path.exists():
        errors = _manifest_validation_errors(candidate_path, packet=packet, project_root=run_dir.parent.parent)
        if errors:
            return errors
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        manifest = ManifestService.normalize_manifest_payload(
            payload,
            run_id=str((packet or {}).get("task_id") or run_dir.name),
        )
        atomic_write_json(manifest_path, manifest.model_dump(exclude_none=True))
        return []
    if manifest_path.exists():
        return _manifest_validation_errors(manifest_path, packet=packet, project_root=run_dir.parent.parent)
    return [f"{candidate_path.name} is missing; manifest.json is missing."]


def _sync_run_generated_scripts(*, run_dir: Path, packet: dict[str, Any] | None) -> None:
    run_id = str((packet or {}).get("task_id") or run_dir.name).strip()
    if not run_id:
        return
    source_root = run_dir / "scripts" / "generated" / run_id
    if not source_root.exists():
        return
    project_root = run_dir.parent.parent
    target_root = project_root / "scripts" / "generated" / run_id
    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _terminal_failure_reason(run_dir: Path) -> str | None:
    terminal_report = _read_json(run_dir / "terminal_report.json", {})
    if isinstance(terminal_report, dict) and terminal_report.get("terminal_kind") in {"report_fail", "synthetic_failure"}:
        reason_code = terminal_report.get("reason_code")
        return reason_code if isinstance(reason_code, str) and reason_code else "unknown"
    return None


def _has_blocking_dependency_issue(run_dir: Path) -> bool:
    if _terminal_failure_reason(run_dir) == "runtime_dependency_missing":
        return True
    issue_path = run_dir / "dependency_issue.json"
    if issue_path.exists():
        try:
            payload = json.loads(issue_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        issues = list(payload.get("issues") or [])
        if any(isinstance(issue, dict) and issue.get("metadata", {}).get("blocking", payload.get("blocking", True)) for issue in issues):
            return True
    brief_path = run_dir / "manager_brief.json"
    if not brief_path.exists():
        return False
    try:
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    issues = list(brief.get("dependency_issues") or [])
    if not issues:
        issues = [
            issue
            for issue in list(brief.get("issues") or [])
            if isinstance(issue, dict) and issue.get("metadata", {}).get("issue_kind") == "runtime_dependency_missing"
        ]
    return any(isinstance(issue, dict) and issue.get("metadata", {}).get("blocking", True) for issue in issues)


def _terminal_report(run_dir: Path) -> dict[str, Any]:
    payload = _read_json(run_dir / "terminal_report.json", {})
    return payload if isinstance(payload, dict) else {}


def _write_manifest_repair_prompt(*, run_dir: Path, packet: dict, errors: list[str], attempt: int) -> Path:
    prompt_path = run_dir / "manifest_repair_prompt.md"
    candidate_path = run_dir / "manifest.candidate.json"
    expected_outputs = packet.get("expected_outputs", [])
    lines = [
        f"You are repairing the manifest for Blueprint run {packet.get('task_id', '')}.",
        "",
        "Do not rerun analysis. Do not change result files, code artifacts, graph files, or input files.",
        f"Rewrite only this file: {candidate_path}",
        "",
        "The previous manifest candidate failed strict schema validation:",
    ]
    lines.extend(f"- {error}" for error in errors)
    missing_output_paths = []
    for item in packet.get("expected_outputs", []):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        path_hint = item.get("path_hint")
        if item.get("required", True) is False:
            continue
        if role and path_hint and any(path_hint in error and "created_assets" in error for error in errors):
            missing_output_paths.append((role, path_hint, item.get("artifact_class")))
    if missing_output_paths:
        lines.extend(
            [
                "",
                "Missing required outputs that must be present on disk and declared in created_assets:",
            ]
        )
        lines.extend(
            f"- {role}: {path_hint} ({output_type or 'unknown type'})"
            for role, path_hint, output_type in missing_output_paths
        )
    lines.extend(
        [
            "",
            "Required manifest JSON shape:",
            "```json",
            json.dumps(
                {
                    "schema_version": "executor_manifest.v2",
                    "summary": "One concise summary sentence.",
                    "created_assets": [
                        {
                            "role": item.get("role"),
                            "path": item.get("path_hint"),
                            "label": item.get("label"),
                            "asset_id": item.get("asset_id"),
                            "description": "Short factual description.",
                        }
                        for item in expected_outputs
                        if item.get("required", True) is not False
                    ],
                    "code_artifacts": [
                        {
                            "path": f"scripts/generated/{packet.get('task_id')}/analysis.R",
                            "language": "R",
                            "purpose": "Reproducible analysis script.",
                        }
                    ],
                    "manager_report": {
                        "summary": "Short summary shown to Manager before reviewer projection.",
                        "warnings": [],
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "Strict rules:",
            "- Use created_assets, not outputs.",
            "- Include summary and manager_report.summary.",
            "- created_assets paths must point to files already produced under allowed result paths.",
            "- created_assets roles must match expected_outputs.role.",
            "- Output file class and format are detected from the file itself and must satisfy the contract.",
            "- Do not add run_id, status, inputs_used, commands_executed, validation_evidence, metrics, key_findings, or top-level warnings.",
        ]
    )
    prompt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        "BP_EVENT "
        + json.dumps(
            {
                "type": "progress_update",
                "stage": "manifest_repair_prompt",
                "progress": None,
                "message": f"Manifest schema validation failed; requesting repair attempt {attempt}.",
                "metadata": {"errors": errors[:5]},
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return prompt_path


def _run_provider_with_repair_loop(
    *,
    command: list[str],
    provider: str,
    packet: dict,
    run_dir: Path,
    project_root: Path,
    env: dict[str, str],
    trace: dict[str, Any],
    started: float,
    trace_started_monotonic: float,
    repair_command_factory: Any | None = None,
    completion_message: str,
    completion_metadata: dict[str, Any],
) -> int:
    worker_timeout = float(os.environ.get("BLUEPRINT_WORKER_TIMEOUT_SECONDS", "1800"))
    repair_timeout = float(os.environ.get("BLUEPRINT_MANIFEST_REPAIR_TIMEOUT_SECONDS", "180"))

    return_code = _run_provider_command(
        command,
        project_root=project_root,
        env=env,
        run_dir=run_dir,
        trace=trace,
        phase="initial_provider",
        attempt=1,
        packet=packet,
        trace_started_monotonic=trace_started_monotonic,
        timeout=worker_timeout,
    )

    terminal_failure_reason = _terminal_failure_reason(run_dir)
    if terminal_failure_reason is not None:
        trace["status"] = "failed"
        trace["current_phase"] = terminal_failure_reason
        trace["finished_at"] = _utc_now()
        trace["total_duration_seconds"] = round(time.monotonic() - started, 3)
        if terminal_failure_reason == "runtime_dependency_missing":
            _record_observation(
                trace,
                "runtime_dependency_missing",
                "Provider reported missing required runtime dependencies; manifest repair was skipped.",
                severity="error",
            )
        else:
            _record_observation(
                trace,
                "terminal_failure_reported",
                f"Provider submitted terminal failure report ({terminal_failure_reason}); manifest repair was skipped.",
                severity="error",
            )
        trace["file_timeline"] = _collect_file_timeline(project_root, run_dir, packet, started_monotonic=started)
        _write_trace(run_dir, trace)
        return 0

    if _has_blocking_dependency_issue(run_dir):
        trace["status"] = "failed"
        trace["current_phase"] = "runtime_dependency_missing"
        trace["finished_at"] = _utc_now()
        trace["total_duration_seconds"] = round(time.monotonic() - started, 3)
        _record_observation(
            trace,
            "runtime_dependency_missing",
            "Provider reported missing required runtime dependencies; manifest repair was skipped.",
            severity="error",
        )
        trace["file_timeline"] = _collect_file_timeline(project_root, run_dir, packet, started_monotonic=started)
        _write_trace(run_dir, trace)
        return 0

    terminal_report = _terminal_report(run_dir)
    if terminal_report.get("terminal_kind") == "report_complete":
        return_code = 0

    initial_timed_out = (return_code == 124)
    candidate_exists = (run_dir / "manifest.candidate.json").exists()

    if return_code == 0 or (initial_timed_out and candidate_exists):
        validation_errors = _promote_candidate_manifest(run_dir=run_dir)
        
        if initial_timed_out:
            trace.setdefault("manifest_validation", []).append(
                {
                    "phase": "initial_provider_timeout",
                    "attempt": 1,
                    "checked_at": _utc_now(),
                    "status": "failed" if validation_errors else "passed",
                    "errors": validation_errors[:10],
                }
            )
            trace["timeout_phase"] = "main_provider"
            if not validation_errors:
                return_code = 0
                trace["repaired_after_main_timeout"] = True
                _record_observation(trace, "repaired_after_main_timeout", "Main analysis timed out, but candidate manifest was successfully validated/promoted.", severity="info")
            trace["file_timeline"] = _collect_file_timeline(project_root, run_dir, packet, started_monotonic=started)
            _write_trace(run_dir, trace)
        else:
            trace.setdefault("manifest_validation", []).append(
                {
                    "phase": "initial_provider",
                    "attempt": 1,
                    "checked_at": _utc_now(),
                    "status": "failed" if validation_errors else "passed",
                    "errors": validation_errors[:10],
                }
            )
            if validation_errors:
                _record_observation(trace, "manifest_validation_failed", "Initial manifest candidate failed schema validation.", severity="warning")
            trace["file_timeline"] = _collect_file_timeline(project_root, run_dir, packet, started_monotonic=started)
            _write_trace(run_dir, trace)

            for attempt in range(1, MAX_MANIFEST_REPAIR_ATTEMPTS + 1):
                if not validation_errors:
                    break
                repair_prompt_path = _write_manifest_repair_prompt(run_dir=run_dir, packet=packet, errors=validation_errors, attempt=attempt)
                repair_command = command
                repair_env = {
                    **env,
                    "BLUEPRINT_EXECUTOR_PROMPT": str(repair_prompt_path),
                    "BLUEPRINT_MANIFEST_REPAIR_PROMPT": str(repair_prompt_path),
                }
                if repair_command_factory is not None:
                    try:
                        repair_command, repair_env = repair_command_factory(repair_prompt_path, repair_env)
                    except Exception as exc:
                        print(f"[wrapper] repair command factory failed: {exc}, using original command", flush=True)

                repair_code = _run_provider_command(
                    repair_command,
                    project_root=project_root,
                    env=repair_env,
                    run_dir=run_dir,
                    trace=trace,
                    phase="manifest_repair",
                    attempt=attempt,
                    packet=packet,
                    trace_started_monotonic=trace_started_monotonic,
                    timeout=repair_timeout,
                )
                if repair_code == 124:
                    trace["timeout_phase"] = "repair_provider"
                    _record_observation(trace, "manifest_repair_timeout", f"Manifest repair attempt {attempt} timed out.", severity="error")
                    return_code = 124
                    break
                if repair_code != 0:
                    return_code = repair_code
                    break
                validation_errors = _promote_candidate_manifest(run_dir=run_dir)
                trace.setdefault("manifest_validation", []).append(
                    {
                        "phase": "manifest_repair",
                        "attempt": attempt,
                        "checked_at": _utc_now(),
                        "status": "failed" if validation_errors else "passed",
                        "errors": validation_errors[:10],
                    }
                )
                trace["file_timeline"] = _collect_file_timeline(project_root, run_dir, packet, started_monotonic=started)
                _write_trace(run_dir, trace)
            if return_code == 0 and validation_errors:
                print("[wrapper] manifest schema validation failed after repair attempts:", flush=True)
                for error in validation_errors:
                    print(f"[wrapper] - {error}", flush=True)
                return_code = 1
                _record_observation(trace, "manifest_repair_exhausted", "Manifest schema validation failed after repair attempts.", severity="error")

    trace["status"] = "success" if return_code == 0 else "failed"
    trace["current_phase"] = "complete"
    trace["finished_at"] = _utc_now()
    trace["total_duration_seconds"] = round(time.monotonic() - started, 3)
    trace["file_timeline"] = _collect_file_timeline(project_root, run_dir, packet, started_monotonic=started)
    if not (run_dir / "manifest.json").exists() and terminal_report.get("terminal_kind") not in {"report_fail", "synthetic_failure"}:
        _record_observation(trace, "manifest_missing", "manifest.json was not produced.", severity="error")
    _write_trace(run_dir, trace)
    print(
        "BP_EVENT "
        + json.dumps(
            {
                "type": "progress_update",
                "stage": "dispatch_complete",
                "progress": 100 if return_code == 0 else None,
                "message": completion_message.format(return_code=return_code),
                "metadata": completion_metadata,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return return_code


class _SetupFailure(Exception):
    """Raised when pre-launch setup (profile / settings / renderer) fails fatally.

    A structured terminal failure report is written *before* raising, so the
    backend surfaces a specific reason_code / summary (e.g. "Executor profile X
    could not be loaded") instead of collapsing the run into a generic
    "executor exit code N" with only a stdout tail. Caught at the single call
    site in main(), which returns ``exit_code``.
    """

    def __init__(self, summary: str, *, exit_code: int = 2) -> None:
        super().__init__(summary)
        self.exit_code = exit_code


def _write_setup_failure(
    *,
    run_dir: Path,
    run_id: str,
    summary: str,
    reason_code: str = "execution_error",
    details: dict[str, Any] | None = None,
) -> None:
    """Write executor_failure.json + terminal_report.json (synthetic_failure).

    Mirrors command_worker's terminal-report contract so worker_service reads a
    specific failure reason rather than synthesizing a generic exit-code error.
    """
    from app.models.runs import ExecutorFailureReport, TerminalReport

    failure = ExecutorFailureReport(
        schema_version="executor_failure.v1",
        reason_code=reason_code,
        summary=summary,
        details=details or {},
    )
    atomic_write_json(run_dir / "executor_failure.json", failure.model_dump())
    report = TerminalReport(
        schema_version="executor_terminal_report.v1",
        run_id=run_id,
        terminal_kind="synthetic_failure",
        accepted_at=_utc_now(),
        summary=summary,
        reason_code=reason_code,
        status="failed",
        failure_report_path="executor_failure.json",
    )
    atomic_write_json(run_dir / "terminal_report.json", report.model_dump(exclude_none=True))


def _try_render_provider(
    *,
    provider: str,
    auth_mode: str | None,
    profile_id: str | None,
    packet: dict,
    run_dir: Path,
    project_root: Path,
) -> tuple[Any | None, Any | None, Any | None, Any | None]:
    """Try to use the renderer registry to produce a provider command/config plan.

    Returns a tuple of (ProviderRenderResult, renderer, profile_spec, settings) if a renderer is available.
    Returns (None, None, None, None) otherwise.

    Setup errors are NOT swallowed into a fabricated profile or a stdout-only
    print. An explicitly-requested profile that fails to load, a failed settings
    load, or a renderer that raises each write a structured terminal failure and
    raise :class:`_SetupFailure` so the backend reports the specific cause. A
    *genuinely* empty profile lookup (no stored profile, no matching builtin
    default, and no error) still falls back to a minimal fabricated spec — the
    legitimate cli_native path.
    """
    if not auth_mode:
        return None, None, None, None
    try:
        from app.workers.provider_renderers import get_renderer_registry
        from app.models.executor_profiles import ExecutorProfileSpec
    except ImportError:
        return None, None, None, None

    registry = get_renderer_registry()
    renderer = registry.get(provider)
    if renderer is None:
        return None, None, None, None

    run_id = str((packet or {}).get("task_id") or run_dir.name)

    profile_spec = None
    try:
        from app.services.app_config_service import AppConfigService
        from app.core.config import get_settings
        config_service = AppConfigService(get_settings())
        stored = config_service.resolve_executor_profile(provider, profile_id=profile_id)
        if stored:
            profile_spec = ExecutorProfileSpec(**stored)
    except Exception as exc:
        # An explicitly-requested profile that fails to load is a hard config
        # error: do NOT fabricate a fake profile over it (that would mask the
        # real cause and resurface later as a generic provider 401 / exit code).
        # With no explicit profile_id we have nothing specific to honour, so we
        # record the error and fall through to the default/fabricate path.
        if profile_id:
            summary = (
                f"Executor profile '{profile_id}' could not be loaded for "
                f"provider={provider}: {exc}"
            )
            print(f"[wrapper] {summary}", flush=True)
            _write_setup_failure(
                run_dir=run_dir,
                run_id=run_id,
                summary=summary,
                details={"provider": provider, "profile_id": profile_id, "phase": "profile_resolution"},
            )
            raise _SetupFailure(summary)
        print(f"[wrapper] executor profile lookup failed for provider={provider}: {exc}", flush=True)

    if profile_spec is None:
        try:
            from app.models.executor_profiles import default_profiles
            defaults = default_profiles()
            profile_spec = next(
                (p for p in defaults if p.worker_type == provider and p.auth_mode == auth_mode),
                None,
            )
        except Exception as exc:
            print(f"[wrapper] default profile lookup failed for provider={provider}: {exc}", flush=True)

    if profile_spec is None:
        profile_spec = ExecutorProfileSpec(
            profile_id=profile_id or f"{provider}-{auth_mode}",
            display_name=f"{provider} ({auth_mode})",
            worker_type=provider,
            auth_mode=auth_mode,
        )

    prompt_path = run_dir / "executor_prompt.md"

    try:
        from app.core.config import get_settings
        settings = get_settings()
    except Exception as exc:
        summary = f"Failed to load settings while preparing provider={provider}: {exc}"
        print(f"[wrapper] {summary}", flush=True)
        _write_setup_failure(
            run_dir=run_dir,
            run_id=run_id,
            summary=summary,
            details={"provider": provider, "phase": "settings_load"},
        )
        raise _SetupFailure(summary)

    try:
        result = renderer.render(
            auth_mode=auth_mode,
            profile=profile_spec,
            prompt_path=prompt_path,
            run_dir=run_dir,
            project_root=project_root,
            settings=settings,
            packet=packet,
        )
    except Exception as exc:
        # A renderer that raises is a real setup failure, not a reason to
        # silently degrade to the legacy template launch (which would hide the
        # root cause behind a generic execution error).
        summary = f"Provider renderer failed for provider={provider}: {exc}"
        print(f"[wrapper] {summary}", flush=True)
        _write_setup_failure(
            run_dir=run_dir,
            run_id=run_id,
            summary=summary,
            details={"provider": provider, "profile_id": profile_id, "phase": "render"},
        )
        raise _SetupFailure(summary)

    try:
        result.write_provider_config_plan(run_dir)
    except Exception as exc:
        print(f"[wrapper] failed to write provider_config_plan.json: {exc}", flush=True)

    return result, renderer, profile_spec, settings


def _run_rendered_provider(
    *,
    renderer_result: Any,
    provider: str,
    auth_mode: str | None,
    profile_id: str | None,
    packet: dict,
    packet_path: Path,
    run_dir: Path,
    project_root: Path,
    env: dict[str, str],
    started: float,
    renderer: Any = None,
    profile_spec: Any = None,
    settings: Any = None,
) -> int:
    """Run the provider command produced by a renderer (instead of the template)."""
    command = renderer_result.command_argv
    merged_env = {**env, **renderer_result.environment_overlay}
    template_preview = " ".join(renderer_result.redacted_command or renderer_result.command_argv)

    trace = _new_trace(
        provider=provider,
        packet=packet,
        run_dir=run_dir,
        project_root=project_root,
        template=template_preview,
        auth_mode=auth_mode,
        profile_id=profile_id,
    )
    trace["renderer"] = {
        "worker_type": renderer_result.worker_type,
        "auth_mode": renderer_result.auth_mode,
        "config_files": list(renderer_result.config_file_paths),
        "config_summary": renderer_result.config_summary,
    }
    _write_trace(run_dir, trace)

    print(
        "BP_EVENT "
        + json.dumps(
            {
                "type": "progress_update",
                "stage": "dispatch",
                "progress": 1,
                "message": f"Dispatching {provider} wrapper (renderer) with unified executor prompt.",
                "metadata": {"provider": provider, "auth_mode": auth_mode, "profile_id": profile_id},
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    def repair_command_factory(repair_prompt_path: Path, repair_env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        if renderer and profile_spec and settings:
            repair_render_result = renderer.render(
                auth_mode=auth_mode or "cli_native",
                profile=profile_spec,
                prompt_path=repair_prompt_path,
                run_dir=run_dir,
                project_root=project_root,
                settings=settings,
                packet=packet,
            )
            if repair_render_result.is_supported and repair_render_result.command_argv:
                return (
                    repair_render_result.command_argv,
                    {
                        **env,
                        **repair_render_result.environment_overlay,
                        "BLUEPRINT_EXECUTOR_PROMPT": str(repair_prompt_path),
                        "BLUEPRINT_MANIFEST_REPAIR_PROMPT": str(repair_prompt_path),
                    },
                )
        return command, repair_env

    return _run_provider_with_repair_loop(
        command=command,
        provider=provider,
        packet=packet,
        run_dir=run_dir,
        project_root=project_root,
        env=merged_env,
        trace=trace,
        started=started,
        trace_started_monotonic=started,
        repair_command_factory=repair_command_factory,
        completion_message=f"{provider} wrapper (renderer) finished with exit code {{return_code}}.",
        completion_metadata={"provider": provider, "auth_mode": auth_mode, "profile_id": profile_id},
    )


def main() -> int:
    args = _parse_args()
    provider = args.provider
    packet_path = Path(args.task_packet)
    run_dir = Path(args.run_dir)
    project_root = Path(args.project_root)
    env = os.environ.copy()
    template = env.get("BLUEPRINT_AGENT_LAUNCH_TEMPLATE", "").strip()
    argv_template_raw = env.get("BLUEPRINT_AGENT_LAUNCH_ARGV_TEMPLATE", "").strip()
    argv_template: list[str] | None = None
    if argv_template_raw:
        try:
            argv_template = json.loads(argv_template_raw)
            if not isinstance(argv_template, list):
                argv_template = None
        except json.JSONDecodeError:
            argv_template = None
    auth_mode = args.auth_mode or env.get("BLUEPRINT_AUTH_MODE", "").strip() or None
    profile_id = args.profile_id or env.get("BLUEPRINT_EXECUTOR_PROFILE_ID", "").strip() or None

    packet = _load_task_packet(packet_path)
    started = time.monotonic()

    try:
        renderer_result, renderer, profile_spec, settings = _try_render_provider(
            provider=provider,
            auth_mode=auth_mode,
            profile_id=profile_id,
            packet=packet,
            run_dir=run_dir,
            project_root=project_root,
        )
    except _SetupFailure as exc:
        # Structured terminal failure already written; the backend will surface
        # the specific reason. Exit non-zero so the run is treated as failed.
        return exc.exit_code

    if renderer_result and renderer_result.unsupported_error:
        print(f"[wrapper] renderer unsupported for provider={provider}: {renderer_result.unsupported_error}", flush=True)
        trace = _new_trace(provider=provider, packet=packet, run_dir=run_dir, project_root=project_root, template=template or "", auth_mode=auth_mode, profile_id=profile_id)
        _write_trace(run_dir, trace)
        trace["status"] = "failed"
        trace["current_phase"] = "renderer_unsupported"
        trace["finished_at"] = _utc_now()
        trace["total_duration_seconds"] = round(time.monotonic() - started, 3)
        _record_observation(trace, "renderer_unsupported", renderer_result.unsupported_error, severity="error")
        _write_trace(run_dir, trace)
        return 2

    if renderer_result and renderer_result.command_argv:
        return _run_rendered_provider(
            renderer_result=renderer_result,
            provider=provider,
            auth_mode=auth_mode,
            profile_id=profile_id,
            packet=packet,
            packet_path=packet_path,
            run_dir=run_dir,
            project_root=project_root,
            env=env,
            started=started,
            renderer=renderer,
            profile_spec=profile_spec,
            settings=settings,
        )
    if renderer_result and renderer_result.is_supported:
        env = {**env, **renderer_result.environment_overlay}

    if not template and not argv_template:
        print(f"[wrapper] missing launch template for provider={provider}", flush=True)
        return 2

    template_preview = " ".join(argv_template) if argv_template else template
    trace = _new_trace(provider=provider, packet=packet, run_dir=run_dir, project_root=project_root, template=template_preview, auth_mode=auth_mode, profile_id=profile_id)
    _write_trace(run_dir, trace)
    print(
        "BP_EVENT "
        + json.dumps(
            {
                "type": "progress_update",
                "stage": "dispatch",
                "progress": 1,
                "message": f"Dispatching {provider} wrapper with unified executor prompt.",
                "metadata": {"provider": provider},
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        if argv_template:
            command = _render_launch_argv_template(
                argv_template,
                provider=provider,
                packet_path=packet_path,
                run_dir=run_dir,
                project_root=project_root,
                packet=packet,
            )
        else:
            command = _render_launch_command(
                template,
                provider=provider,
                packet_path=packet_path,
                run_dir=run_dir,
                project_root=project_root,
                packet=packet,
            )
    except RuntimeError as exc:
        print(f"[wrapper] {exc}", flush=True)
        trace["status"] = "failed"
        trace["current_phase"] = "launch_template_error"
        trace["finished_at"] = _utc_now()
        trace["total_duration_seconds"] = round(time.monotonic() - started, 3)
        _record_observation(trace, "launch_template_error", str(exc), severity="error")
        _write_trace(run_dir, trace)
        return 2
    def repair_command_factory(repair_prompt_path: Path, repair_env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        if argv_template:
            repair_command = _render_launch_argv_template(
                argv_template,
                provider=provider,
                packet_path=packet_path,
                run_dir=run_dir,
                project_root=project_root,
                prompt_path=repair_prompt_path,
                packet=packet,
            )
        else:
            repair_command = _render_launch_command(
                template,
                provider=provider,
                packet_path=packet_path,
                run_dir=run_dir,
                project_root=project_root,
                prompt_path=repair_prompt_path,
                packet=packet,
            )
        return repair_command, repair_env

    return _run_provider_with_repair_loop(
        command=command,
        provider=provider,
        packet=packet,
        run_dir=run_dir,
        project_root=project_root,
        env=env,
        trace=trace,
        started=started,
        trace_started_monotonic=started,
        repair_command_factory=repair_command_factory,
        completion_message=f"{provider} wrapper finished with exit code {{return_code}}.",
        completion_metadata={"provider": provider, "input_assets": len(packet.get("input_assets", []))},
    )


if __name__ == "__main__":
    raise SystemExit(main())
