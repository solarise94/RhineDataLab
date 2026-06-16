from __future__ import annotations

import json
import platform
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.app_config_service import AppConfigService
from app.services.project_service import ProjectService
from app.services.utils import utc_now
from app.services.worker_service import WorkerService


SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|credential)", re.IGNORECASE)
SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]+|gh[opsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")
HOME_PATH_RE = re.compile(r"/home/[^/\s]+")

# Run-file collection: extend this set when new diagnostic file types appear.
_RUN_FILE_EXTENSIONS: frozenset[str] = frozenset({".json", ".md", ".log", ".jsonl", ".py"})
_MAX_RUN_FILE_BYTES = 2 * 1024 * 1024  # 2 MB per file; larger files are omitted.
_MAX_BUNDLE_RETAIN = 10  # Keep only the N most recent bundles per project.
_MANAGER_PROBE_TIMEOUT_SECONDS = 2


class DiagnosticBundleService:
    def __init__(
        self,
        project_service: ProjectService,
        app_config_service: AppConfigService,
        worker_service: WorkerService,
        settings: Settings,
    ) -> None:
        self.project_service = project_service
        self.app_config_service = app_config_service
        self.worker_service = worker_service
        self.settings = settings

    def build_bundle(self, project_id: str, *, max_runs: int = 8) -> dict[str, Any]:
        project_root = self.project_service.project_path(project_id)
        snapshot = self.project_service.get_project_snapshot(project_id)
        files_payload = self._list_run_file_payload(project_id, max_runs=max_runs)
        sessions = self._chat_sessions(project_id)  # cache once, reuse below
        bundle_dir = project_root / "reports" / "diagnostics"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        timestamp = utc_now().replace(":", "").replace("-", "")
        bundle_name = f"{project_id}_diagnostic_bundle_{timestamp}_{time.monotonic_ns():x}.zip"
        bundle_path = bundle_dir / bundle_name

        with tempfile.TemporaryDirectory(prefix="diagnostic-bundle-") as tmp_dir_raw:
            tmp_dir = Path(tmp_dir_raw)
            payload_root = tmp_dir / f"{project_id}_diagnostic_bundle"
            payload_root.mkdir(parents=True, exist_ok=True)
            self._write_json(payload_root / "manifest.json", self._manifest(project_id, snapshot, files_payload, sessions))
            self._write_json(payload_root / "system_info.json", self._system_info(project_id))
            self._write_json(payload_root / "project_snapshot.json", self._sanitize(snapshot))
            self._write_json(payload_root / "app_settings_summary.json", self._app_settings_summary())
            self._write_json(payload_root / "sessions.json", sessions)
            self._write_json(payload_root / "recent_errors.json", self._recent_errors(snapshot, files_payload))
            self._write_run_files(payload_root / "runs", project_root, files_payload)
            self._zip_dir(payload_root, bundle_path)

        self._prune_old_bundles(bundle_dir, keep=_MAX_BUNDLE_RETAIN)
        relative_posix = bundle_path.relative_to(project_root).as_posix()
        bundle_size = bundle_path.stat().st_size

        return {
            "path": relative_posix,
            "download_url": f"/api/projects/{project_id}/diagnostics/download?path={urllib.parse.quote(relative_posix, safe='/')}",
            "created_at": utc_now(),
            "run_count": len(files_payload["runs"]),
            "session_count": len(sessions.get("items", [])),
            "bundle_size": bundle_size,
            "bundle_size_label": self._format_size(bundle_size),
        }

    def _manifest(self, project_id: str, snapshot: dict[str, Any], files_payload: dict[str, Any], sessions: dict[str, Any]) -> dict[str, Any]:
        summary = snapshot["summary"]
        return {
            "kind": "project_diagnostic_bundle",
            "project_id": project_id,
            "created_at": utc_now(),
            "project": {
                "name": summary.name,
                "status": summary.status,
                "current_goal": summary.current_goal,
                "card_counts": summary.card_counts,
                "result_counts": summary.result_counts,
            },
            "includes": {
                "system_info": True,
                "project_snapshot": True,
                "app_settings_summary": True,
                "chat_sessions": True,
                "recent_errors": True,
                "run_directories": [item["run_id"] for item in files_payload["runs"]],
                "session_count": len(sessions.get("items", [])),
            },
            "redaction": {
                "api_keys": True,
                "tokens": True,
                "home_paths": True,
            },
        }

    def _app_settings_summary(self) -> dict[str, Any]:
        public = self.app_config_service.get_public_settings()
        secret = self.app_config_service.get_secret_settings()
        return self._sanitize(
            {
                "public": public,
                "effective": {
                    "manager_model": secret.get("manager_model"),
                    "executor_model": secret.get("executor_model"),
                    "reviewer_model": secret.get("reviewer_model"),
                    "library_summarizer_model": secret.get("library_summarizer_model"),
                    "manager_websearch_enabled": secret.get("manager_websearch_enabled"),
                    "deepseek_api_base_url": secret.get("deepseek_api_base_url"),
                    "pi_deepseek_base_url": secret.get("pi_deepseek_base_url"),
                    "tavily_base_url": secret.get("tavily_base_url"),
                },
            }
        )

    def _chat_sessions(self, project_id: str) -> dict[str, Any]:
        sessions = self.project_service.graph_store(project_id).load_chat_sessions()
        payload = {
            "items": [
                {
                    "session_id": item.session_id,
                    "summary": item.summary,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "messages": [message.model_dump() for message in item.messages],
                }
                for item in sessions
            ]
        }
        return self._sanitize(payload)

    def _recent_errors(self, snapshot: dict[str, Any], files_payload: dict[str, Any]) -> dict[str, Any]:
        failed_cards = []
        for card in snapshot["cards"]:
            if getattr(card, "status", None) in {"failed", "cancelled", "needs_review", "reviewing"}:
                failed_cards.append(
                    {
                        "card_id": card.card_id,
                        "title": card.title,
                        "status": card.status,
                        "summary": card.summary,
                        "linked_runs": card.linked_runs,
                    }
                )
        run_errors = []
        for run in files_payload["runs"]:
            status = str(run.get("status") or "")
            if status not in {"failed", "cancelled", "reviewing", "needs_review"}:
                continue
            run_errors.append(
                {
                    "run_id": run["run_id"],
                    "card_id": run.get("card_id"),
                    "status": status,
                    "error_hint": run.get("error_hint"),
                    "files": [file["path"] for file in run["files"]],
                }
            )
        return self._sanitize({"cards": failed_cards, "runs": run_errors})

    def _list_run_file_payload(self, project_id: str, *, max_runs: int) -> dict[str, Any]:
        project_root = self.project_service.project_path(project_id)
        graph = self.project_service.graph_store(project_id).load_graph()
        runs = sorted(
            graph.runs,
            key=lambda item: (item.finished_at or item.started_at or "", item.run_id),
            reverse=True,
        )[:max_runs]
        entries = []
        for run in runs:
            run_dir = project_root / "runs" / run.run_id
            files = []
            if run_dir.exists():
                for path in sorted(run_dir.rglob("*")):
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in _RUN_FILE_EXTENSIONS:
                        continue
                    rel_path = path.relative_to(project_root).as_posix()
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    if size > _MAX_RUN_FILE_BYTES:
                        files.append({
                            "path": rel_path,
                            "filename": path.name,
                            "omitted": True,
                            "reason": "size_limit",
                            "size": size,
                        })
                    else:
                        files.append({"path": rel_path, "filename": path.name})
            entries.append(
                {
                    "run_id": run.run_id,
                    "card_id": run.card_id,
                    "status": run.status,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "error_hint": getattr(run, "error", None) or getattr(run, "summary", None),
                    "files": files,
                }
            )
        return {"runs": entries}

    def _write_run_files(self, target_root: Path, project_root: Path, files_payload: dict[str, Any]) -> None:
        for run in files_payload["runs"]:
            run_target = target_root / run["run_id"]
            run_target.mkdir(parents=True, exist_ok=True)
            omitted_files = [
                {"path": file_entry["path"], "size": file_entry.get("size"), "reason": file_entry.get("reason")}
                for file_entry in run["files"]
                if file_entry.get("omitted")
            ]
            meta_payload = {k: v for k, v in run.items() if k != "files"}
            meta_payload["omitted_files"] = omitted_files
            self._write_json(run_target / "_meta.json", self._sanitize(meta_payload))
            for file_entry in run["files"]:
                if file_entry.get("omitted"):
                    continue  # recorded in _meta payload via files list, content skipped
                source = project_root / file_entry["path"]
                relative_name = Path(file_entry["filename"])
                content = self._read_text_or_json(source)
                if isinstance(content, (dict, list)):
                    self._write_json(run_target / relative_name, self._sanitize(content))
                else:
                    (run_target / relative_name).write_text(self._sanitize_text(content), encoding="utf-8")

    def _read_text_or_json(self, path: Path) -> Any:
        if path.suffix in {".json", ".jsonl"}:
            if path.suffix == ".jsonl":
                lines = [self._sanitize_text(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
                return {"lines": lines}
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return path.read_text(encoding="utf-8", errors="replace")
        return path.read_text(encoding="utf-8", errors="replace")

    def _zip_dir(self, source_root: Path, bundle_path: Path) -> None:
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_root.rglob("*")):
                if path.is_file():
                    archive.write(path, arcname=path.relative_to(source_root.parent).as_posix())

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if SENSITIVE_KEY_RE.search(str(key)):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = self._sanitize(item)
            return result
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if hasattr(value, "model_dump"):
            return self._sanitize(value.model_dump())
        if isinstance(value, str):
            return self._sanitize_text(value)
        return value

    def _sanitize_text(self, text: str) -> str:
        redacted = SECRET_VALUE_RE.sub("[REDACTED]", text)
        redacted = HOME_PATH_RE.sub("/home/[user]", redacted)
        redacted = re.sub(r"(?i)(api[_-]?key|token|secret|password)(\s*[=:]\s*)([^\s\"']+)", r"\1\2[REDACTED]", redacted)
        return redacted

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _system_info(self, project_id: str) -> dict[str, Any]:
        return self._sanitize(
            {
                "backend_version": self._backend_version(),
                "os": self._os_info(),
                "python": self._python_info(),
                "bwrap": self._bwrap_info(),
                "python_runtimes": self.project_service.get_python_runtimes(),
                "r_runtimes": self.project_service.get_r_runtimes(),
                "providers": self._provider_info(),
                "manager_agent": self._probe_manager_agent(),
                "worker": self.worker_service.diagnostic_run_state(project_id),
            }
        )

    def _backend_version(self) -> str:
        try:
            return _pkg_version("blueprint-re-backend")
        except PackageNotFoundError:
            # Fallback: read pyproject.toml statically.
            import tomllib
            toml_path = Path(__file__).resolve().parents[3] / "pyproject.toml"
            if toml_path.exists():
                try:
                    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
                    return str(data.get("project", {}).get("version", "unknown"))
                except Exception:
                    pass
            return "unknown"

    def _os_info(self) -> dict[str, Any]:
        try:
            uname = platform.uname()
            return {
                "platform": platform.platform(),
                "machine": uname.machine,
                "release": uname.release,
                "system": uname.system,
                "node": uname.node,  # hostname; _sanitize will NOT redact this — see note
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _python_info(self) -> dict[str, str]:
        return {"version": sys.version, "executable": sys.executable}

    def _bwrap_info(self) -> dict[str, Any]:
        from app.workers.command_worker import _BWRAP_SMOKE_CACHE, _ensure_bwrap_runtime

        configured_mode = str(getattr(self.settings, "executor_sandbox_mode", "none"))
        bwrap_path = shutil.which("bwrap")
        installed = bwrap_path is not None
        smoke_ok = False
        smoke_error = None
        try:
            resolved = _ensure_bwrap_runtime()
            smoke_ok = True
            bwrap_path = bwrap_path or resolved
        except Exception as exc:
            smoke_error = str(exc)
        # Cache is keyed by resolved bwrap path; report whether any prior smoke
        # test in this process succeeded.
        previously_smoke_ok = any(_BWRAP_SMOKE_CACHE.values())
        return {
            "configured_mode": configured_mode,
            "installed": installed,
            "smoke_ok": smoke_ok,
            "path": bwrap_path,
            # Cached result from a previous smoke test during this process lifetime.
            # Note: if bwrap is fixed at runtime, this cache is stale until restart.
            "previously_smoke_ok": previously_smoke_ok,
            "error": smoke_error,
        }

    def _provider_info(self) -> dict[str, Any]:
        public = self.app_config_service.get_public_settings()
        return {
            "deepseek": {
                "api_key_configured": bool(public.get("deepseek", {}).get("api_key_configured")),
                "base_url": public.get("deepseek", {}).get("api_base_url"),
            },
            "tavily": {
                "api_key_configured": bool(public.get("web_search", {}).get("api_key_configured")),
                "base_url": public.get("web_search", {}).get("base_url"),
            },
            "anthropic": {
                "api_key_configured": bool(public.get("anthropic", {}).get("api_key_configured")),
                "base_url": public.get("anthropic", {}).get("api_base_url"),
            },
            "openai": {
                "api_key_configured": bool(public.get("openai", {}).get("api_key_configured")),
                "base_url": public.get("openai", {}).get("api_base_url"),
            },
            "provider_bindings": public.get("provider_bindings"),
            "api_provider_profiles": public.get("api_provider_profiles"),
        }

    def _probe_manager_agent(self) -> dict[str, Any]:
        url = str(getattr(self.settings, "pi_manager_url", "http://127.0.0.1:18002")).rstrip("/")
        health_url = f"{url}/healthz"
        result: dict[str, Any] = {"url": url, "reachable": False, "status_code": None, "latency_ms": None, "error": None}
        try:
            start = time.monotonic()
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=_MANAGER_PROBE_TIMEOUT_SECONDS) as resp:
                result["status_code"] = resp.status
                result["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
                result["reachable"] = 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            result["status_code"] = exc.code
            result["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
            result["reachable"] = False
            result["error"] = str(exc)
        except Exception as exc:
            result["reachable"] = False
            result["error"] = str(exc)
        return result

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        value = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{value:.1f} GB"

    def _prune_old_bundles(self, bundle_dir: Path, *, keep: int) -> None:
        """Keep only the N most recent diagnostic bundles; delete older ones."""
        try:
            bundles = [p for p in bundle_dir.glob("*.zip") if p.is_file()]
        except OSError:
            return
        if len(bundles) <= keep:
            return
        bundles.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in bundles[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass
