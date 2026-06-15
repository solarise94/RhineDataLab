# 诊断功能增强实施计划

> 本文档是可直接交付实施的细化计划。面向"按文件逐项落地"，每节给出：现状（含 `file:line`）、目标、改动要点、验收点。
>
> 实施顺序：后端服务层 → 后端依赖与 API → main.py → 前端 → 测试 → 验证。建议严格按顺序执行，因为后端测试和前端类型都依赖前序改动。

## 目标

把当前"诊断包导出"功能从「会话 + 最近 run 日志 + 错误摘要」补强为「系统信息 + 会话 + 动态采集的 run 日志 + 错误摘要」，并修复若干实现缺陷，使诊断包在排查"为什么执行失败"时真正够用。

## 现状基线（读这些代码先）

- 后端服务：`backend/app/services/diagnostic_bundle_service.py`（260 行）
- 后端 API：`backend/app/api/diagnostics.py`（37 行）
- 依赖工厂：`backend/app/api/deps.py:220-225`
- 前端入口：`frontend/components/settings/SettingsPanels.tsx:1191-1216`
- 前端类型：`frontend/lib/types.ts:719-725`
- 前端 API：`frontend/lib/api.ts:742-746`
- healthz：`backend/app/main.py:91-93`

已知缺陷（本计划修复）：

1. 完全没有系统/环境信息（版本、OS、bwrap、runtime、provider 配置状态、manager-agent 在线、worker 队列）。
2. run 文件是 25 项硬编码白名单（`diagnostic_bundle_service.py:159-185`），新增产物易漏。
3. `_chat_sessions(project_id)` 被调用两次（`:43` 写文件、`:53` 算 count），大会话双倍开销。
4. `download_url` 的 path 未 URL 编码（`:50`）。
5. 历史 bundle 无清理，`reports/diagnostics/` 无限累积。
6. `healthz` 只返回 `{status:"ok"}`，价值有限。
7. 前端下载链接无文件大小展示。

## 设计约束（不可违反）

- **不加鉴权**：path-traversal 校验（`diagnostics.py:31-36`）已存在，鉴权是独立产品决策。
- **不改 manager-agent**：它已有 `/healthz`（`manager-agent/src/server.js:3690`），本次只加后端探活。
- **不动 schema 生成**：诊断包是运行时导出，不涉及 Pydantic JSON schema，无需跑 `generate_backend_schemas.py`。
- **httpx 不是后端依赖**（已核实 `backend/pyproject.toml` 无 httpx/requests），manager-agent 探活用标准库 `urllib.request`。
- **遵循现有代码风格**：Python 4 空格 + type hints + snake_case + 小函数；TS 2 空格 + 双引号 + PascalCase/camelCase。

---

## 步骤 1：ProjectService 新增公开 runtime 方法

**文件**：`backend/app/services/project_service.py`

**现状**：`_python_runtimes()`（`:1060`）和 `_r_runtimes()`（`:1104`）是私有方法，返回 `list[dict]`，每项 `{name, label, path, manager, exists}`。

**改动**：在 `_r_runtimes` 方法之后（约 `:1156` 前），新增两个公开方法：

```python
def get_python_runtimes(self) -> list[dict]:
    """Public accessor for diagnostic/export consumers."""
    return self._python_runtimes()

def get_r_runtimes(self) -> list[dict]:
    """Public accessor for diagnostic/export consumers."""
    return self._r_runtimes()
```

**验收**：公开方法存在且返回与私有方法一致。

---

## 步骤 2：WorkerService 新增 diagnostic_run_state 公开方法

**文件**：`backend/app/services/worker_service.py`

**现状**：
- `get_available_run_slots(project_id) -> int`（`:1506`），读 `Semaphore._value`。
- `_active_run_statuses() -> set`（`:2407`）返回 `{"queued","launching","needs_approval","running","reviewing"}`。
- `has_active_runs(project_id) -> bool`（`:2421`）展示了"加锁 + load_runs + 过滤"的标准模式。
- 内部状态：`self._threads: dict[str, Thread]`（`:162`）、`self.project_service.settings.executor_max_concurrent_runs`。

**改动**：在 `has_active_runs` 之后新增公开方法：

```python
def diagnostic_run_state(self, project_id: str) -> dict:
    """Return worker/queue state for diagnostic bundle. Best-effort, never raises."""
    active_statuses = self._active_run_statuses()
    max_concurrent = int(getattr(self.project_service.settings, "executor_max_concurrent_runs", 3))
    try:
        available_slots = int(self.get_available_run_slots(project_id))
    except Exception:
        available_slots = None
    try:
        lock = self.project_service.lock_for(project_id)
        with lock:
            runs = self.project_service.graph_store(project_id).load_runs()
        active = [run for run in runs if run.status in active_statuses]
    except Exception:
        active = []
    active_ids = [run.run_id for run in active]
    stuck_ids = []
    for run in active:
        thread = self._threads.get(run.run_id)
        if thread is None or not thread.is_alive():
            stuck_ids.append(run.run_id)
    return {
        "max_concurrent": max_concurrent,
        "available_slots": available_slots,
        "active_run_ids": active_ids,
        "stuck_run_ids": stuck_ids,
    }
```

**注意**：`_threads` 是私有属性，但诊断采集访问它是可接受的——本方法把访问收敛到一处，避免诊断服务直接碰私有属性。

**验收**：方法返回 dict 含四个键；正常无活跃 run 时 `active_run_ids`/`stuck_run_ids` 为空列表。

---

## 步骤 3：重写 DiagnosticBundleService

**文件**：`backend/app/services/diagnostic_bundle_service.py`

这是本计划核心改动，分多个子项。

### 3.1 导入与常量

**现状导入**（`:1-13`）：
```python
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any
```

**改为**（新增 `importlib.metadata`、`platform`、`sys`、`shutil`、`time`、`urllib.parse`、`urllib.request`，并引入 `Settings`、`WorkerService` 类型）：

```python
from __future__ import annotations

import json
import os
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
```

**新增模块级常量**（在现有三个正则常量之后，`:18` 之后）：

```python
# Run-file collection: extend this set when new diagnostic file types appear.
_RUN_FILE_EXTENSIONS: frozenset[str] = frozenset({".json", ".md", ".log", ".jsonl", ".py"})
_MAX_RUN_FILE_BYTES = 2 * 1024 * 1024  # 2 MB per file; larger files are omitted.
_MAX_BUNDLE_RETAIN = 10  # Keep only the N most recent bundles per project.
_MANAGER_PROBE_TIMEOUT_SECONDS = 2
```

### 3.2 构造函数扩展

**现状**（`:22-24`）：
```python
def __init__(self, project_service: ProjectService, app_config_service: AppConfigService) -> None:
    self.project_service = project_service
    self.app_config_service = app_config_service
```

**改为**：
```python
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
```

### 3.3 build_bundle 主体重写

**现状**（`:26-54`）：调 `_chat_sessions` 两次；URL 不编码；无清理；返回值无 size。

**改为**（关键点：缓存 sessions；写完 zip 后 prune；URL 编码；返回 size）：

```python
def build_bundle(self, project_id: str, *, max_runs: int = 8) -> dict[str, Any]:
    project_root = self.project_service.project_path(project_id)
    snapshot = self.project_service.get_project_snapshot(project_id)
    files_payload = self._list_run_file_payload(project_id, max_runs=max_runs)
    sessions = self._chat_sessions(project_id)  # cache once, reuse below
    bundle_dir = project_root / "reports" / "diagnostics"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now().replace(":", "").replace("-", "")
    bundle_name = f"{project_id}_diagnostic_bundle_{timestamp}.zip"
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
```

### 3.4 _manifest 增加 system_info 标记

**现状**（`:56-81`）：`includes` 里没有 `system_info`。

**改动**：`_manifest` 签名加 `sessions` 参数（用于把 session_count 也写进 manifest，避免再算一次），`includes` 加 `"system_info": True`：

```python
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
```

### 3.5 新增 _system_info（核心）

新增方法，采集系统/环境信息。全部经 `_sanitize()` 脱敏。各子项用独立 try/except，单个失败不影响整体。

```python
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
```

**注意 `node`（hostname）**：`_sanitize` 只脱敏 home 路径和密钥模式，不会动 hostname。如果产品要求 hostname 也脱敏，在 `_system_info` 返回前对 `os.node` 做替换（如 `"node": "[redacted]"`）。默认保留——hostname 对排查有价值且不算 secret。

```python
def _bwrap_info(self) -> dict[str, Any]:
    from app.workers.command_worker import _BWRAP_SMOKE_OK, _ensure_bwrap_runtime

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
    return {
        "configured_mode": configured_mode,
        "installed": installed,
        "smoke_ok": smoke_ok,
        "path": bwrap_path,
        # Cached result from a previous smoke test during this process lifetime.
        # Note: if bwrap is fixed at runtime, this cache is stale until restart.
        "previously_smoke_ok": _BWRAP_SMOKE_OK,
        "error": smoke_error,
    }
```

**为什么 try `_ensure_bwrap_runtime()` 而不只读缓存**：`_ensure_bwrap_runtime()`（`command_worker.py:20`）内部有 `_BWRAP_SMOKE_OK` 缓存，若已成功则直接返回不重跑 smoke；若为 None 才跑一次。诊断调用它既得到当前准确结果，又复用缓存，不会重复开销。失败时记录 error，不抛。

```python
def _provider_info(self) -> dict[str, Any]:
    public = self.app_config_service.get_public_settings()
    return {
        "deepseek": {
            "api_key_configured": bool(public.get("deepseek", {}).get("api_key_configured")),
            "base_url": public.get("deepseek", {}).get("base_url"),
        },
        "tavily": {
            "api_key_configured": bool(public.get("web_search", {}).get("api_key_configured")),
            "base_url": public.get("web_search", {}).get("base_url"),
        },
        "anthropic": {
            "api_key_configured": bool(public.get("anthropic", {}).get("api_key_configured")),
            "base_url": public.get("anthropic", {}).get("base_url"),
        },
        "openai": {
            "api_key_configured": bool(public.get("openai", {}).get("api_key_configured")),
            "base_url": public.get("openai", {}).get("base_url"),
        },
        "provider_bindings": public.get("provider_bindings"),
        "api_provider_profiles": public.get("api_provider_profiles"),
    }
```

**注意**：`get_public_settings()` 的返回结构需在实现时与 `app_config_service.py:47-92` 的实际键名核对（上面是基于探索结论的预期结构）。如果 `deepseek` 段的键名不同，按实际为准。只读 `api_key_configured` 布尔和 base_url，**不**调用 `test_api_provider`（避免触发真实网络请求）。

```python
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
```

**注意**：`_probe_manager_agent` 的超时 2s 是阻塞的，会在导出请求的同步路径上叠加最多 2s。这是可接受的（诊断导出本身非高频）。

### 3.6 新增 _format_size

```python
@staticmethod
def _format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
```

### 3.7 重写 _list_run_file_payload（动态采集）

**现状**（`:147-200`）：硬编码 25 项文件名循环。

**改为**：遍历 `run_dir.rglob("*")`，按扩展名白名单过滤，按大小标记 omitted。

```python
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
```

### 3.8 _write_run_files 支持 omitted

**现状**（`:202-214`）：无差别写每个文件。

**改为**：跳过 `omitted` 文件的内容写入。

```python
def _write_run_files(self, target_root: Path, project_root: Path, files_payload: dict[str, Any]) -> None:
    for run in files_payload["runs"]:
        run_target = target_root / run["run_id"]
        run_target.mkdir(parents=True, exist_ok=True)
        self._write_json(run_target / "_meta.json", self._sanitize({k: v for k, v in run.items() if k != "files"}))
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
```

**注意**：omitted 文件的元信息（path/reason/size）已经存在于 `run["files"]` 列表里。如果希望 omitted 信息也出现在 `_meta.json`，可在 `_meta.json` 写入时保留一个 `omitted_files` 摘要。当前设计是 omitted 信息只在 `files` 列表（不写入 zip 内容），但 `files` 列表本身不单独落盘。**可选增强**：在 `_meta.json` 里追加 `"omitted_files": [{"path":..., "size":...}, ...]`，便于不打开 run 目录就看到哪些文件被省略。建议实现时加这个增强。

### 3.9 新增 _prune_old_bundles

```python
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
```

**已知限制**：无并发锁。两个并发导出可能在 `iterdir`/`unlink` 间产生竞争。诊断导出是低频操作，暂不处理；如需严谨可加项目级文件锁。

### 3.10 其余方法不变

`_app_settings_summary`（`:83`）、`_chat_sessions`（`:102`）、`_recent_errors`（`:118`）、`_read_text_or_json`（`:216`）、`_zip_dir`（`:227`）、`_sanitize`（`:233`）、`_sanitize_text`（`:250`）、`_write_json`（`:256`）保持不变。

---

## 步骤 4：更新依赖工厂

**文件**：`backend/app/api/deps.py:220-225`

**现状**：
```python
@lru_cache
def get_diagnostic_bundle_service() -> DiagnosticBundleService:
    return DiagnosticBundleService(
        get_project_service(),
        get_app_config_service(),
    )
```

**改为**：
```python
@lru_cache
def get_diagnostic_bundle_service() -> DiagnosticBundleService:
    return DiagnosticBundleService(
        get_project_service(),
        get_app_config_service(),
        get_worker_service(),
        get_settings(),
    )
```

`get_settings` 已在文件顶部导入（`:3` `from app.core.config import get_settings`）。`get_worker_service` 已定义（`:106`，有 `@lru_cache`）。

**验收**：`curl -X POST http://127.0.0.1:18001/api/projects/<seed>/diagnostics/export?max_runs=2` 不报 500。

---

## 步骤 5：healthz 增强

**文件**：`backend/app/main.py`

**现状**（`:91-93`）：
```python
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
```

**改动**：在 `logger = logging.getLogger(__name__)`（`:12`）之后加模块级变量，并重写 healthz。

模块级（约 `:13` 后）：
```python
import time

_STARTED_MONOTONIC = time.monotonic()
```
（注意：`main.py` 顶部目前没有 `import time`，需新增。放在现有 import 区，按字母序。`import logging` 在 `:1`，`from contextlib import asynccontextmanager` 在 `:2`，把 `import time` 加在 `import logging` 之后。）

healthz 重写：
```python
@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "backend_version": _backend_version(),
        "uptime_seconds": round(time.monotonic() - _STARTED_MONOTONIC, 1),
        "sandbox_mode": getattr(settings, "executor_sandbox_mode", "unknown"),
    }


def _backend_version() -> str:
    try:
        from importlib.metadata import version, PackageNotFoundError
        return version("blueprint-re-backend")
    except Exception:
        return "unknown"
```

**用 `time.monotonic()` 而非 `utc_now()` 差值**：避免系统时钟跳变（NTP 校准）导致 uptime 为负或跳变。

**验收**：`curl -s http://127.0.0.1:18001/healthz` 返回四个键。

---

## 步骤 6：前端类型 + API

**文件**：`frontend/lib/types.ts:719-725`

**现状**：
```ts
export interface DiagnosticExportResponse {
  path: string;
  download_url: string;
  created_at: string;
  run_count: number;
  session_count: number;
}
```

**改为**（新增两个可选字段）：
```ts
export interface DiagnosticExportResponse {
  path: string;
  download_url: string;
  created_at: string;
  run_count: number;
  session_count: number;
  bundle_size?: number;
  bundle_size_label?: string;
}
```

**文件**：`frontend/lib/api.ts:742-746`

**无需改动**：`exportDiagnostics` 是 `POST`，响应自动包含新字段。

---

## 步骤 7：前端 UI 加文件大小行

**文件**：`frontend/components/settings/SettingsPanels.tsx:1208-1215`

**现状**：
```tsx
{diagnosticInfo ? (
  <div className="settings-kv-list">
    <div><strong>导出时间</strong><span>{diagnosticInfo.created_at}</span></div>
    <div><strong>包含 runs</strong><span>{diagnosticInfo.run_count}</span></div>
    <div><strong>包含 sessions</strong><span>{diagnosticInfo.session_count}</span></div>
    <div><strong>保存路径</strong><span>{diagnosticInfo.path}</span></div>
  </div>
) : null}
```

**改为**（在"保存路径"行后加"文件大小"行）：
```tsx
{diagnosticInfo ? (
  <div className="settings-kv-list">
    <div><strong>导出时间</strong><span>{diagnosticInfo.created_at}</span></div>
    <div><strong>包含 runs</strong><span>{diagnosticInfo.run_count}</span></div>
    <div><strong>包含 sessions</strong><span>{diagnosticInfo.session_count}</span></div>
    <div><strong>文件大小</strong><span>{diagnosticInfo.bundle_size_label ?? "—"}</span></div>
    <div><strong>保存路径</strong><span>{diagnosticInfo.path}</span></div>
  </div>
) : null}
```

**验收**：`npm run build` 通过；导出后 UI 显示文件大小。

---

## 步骤 8：测试

**文件**：`backend/tests/test_diagnostic_bundle.py`（新建）

风格遵循 `test_worker_service.py`：`unittest.TestCase` + `tempfile.mkdtemp` + 真实 `Settings` + `get_settings.cache_clear()`。

**测试基建**（setUp/tearDown）：
- `self.tmpdir = tempfile.mkdtemp(prefix="diag-test-")`
- `self.settings = Settings(data_root=Path(self.tmpdir), executor_sandbox_mode="none")`，`get_settings.cache_clear()`
- 构造 `ProjectService`、`AppConfigService`、`WorkerService`、`DiagnosticBundleService`
- 用 `project_service` 创建一个 seed 项目，往 `runs/<run_id>/` 放假文件
- `tearDown`：`shutil.rmtree(self.tmpdir, ignore_errors=True)` + `get_settings.cache_clear()`

**用例清单**：

| 用例 | 做法 | 断言 |
|---|---|---|
| `test_bundle_contains_system_info` | 调 `build_bundle`，解压 zip | `system_info.json` 存在；含 `backend_version/os/python/bwrap/python_runtimes/r_runtimes/providers/manager_agent/worker` 键；`manager_agent.reachable` 键存在（不要求 true，测试环境 manager-agent 可能没起） |
| `test_dynamic_run_file_collection` | run 目录放 `custom_trace.json`（不在原 25 项硬编码） + `artifact.bin`（非白名单扩展名） | 解压后 `runs/<run_id>/custom_trace.json` 存在；`artifact.bin` 不存在 |
| `test_large_run_file_omitted` | 放一个 3MB 的 `.json` | zip 中无该文件内容；`_meta.json` 的 `omitted_files` 摘要含该文件 |
| `test_bundle_prune` | 连续调 `build_bundle` 12 次 | `reports/diagnostics/*.zip` 恰好 10 个 |
| `test_download_url_encoded` | 调一次 `build_bundle`，检查返回 `download_url` | path 段为 percent-encoded 形式（断言 `quote(..., safe="/")` 的输出特征，如不含原始特殊字符） |
| `test_session_count_single_load` | monkeypatch/spy `_chat_sessions` 计数 | 一次 `build_bundle` 中 `_chat_sessions` 恰好调用 1 次 |

**关键技术点**：
- 解压用 `zipfile.ZipFile(bundle_path).extractall(extract_dir)`，然后读 `extract_dir/<project_id>_diagnostic_bundle/system_info.json`。
- `test_session_count_single_load`：用 `unittest.mock.patch.object(service, "_chat_sessions", wraps=service._chat_sessions)` 包一层，断言 `mock.call_count == 1`。
- manager-agent 探活无需 mock：测试环境无 manager-agent 时返回 `reachable:false`，断言键存在即可。

**注意 `Settings` 的必填字段**：参考 `test_worker_service.py:17-25` 的构造方式，可能需要传 `executor_conda_base` 等字段。如果 `Settings()` 构造报缺字段，照 `test_worker_service.py` 的实际写法补全。

---

## 步骤 9：验证

按 `AGENTS.md` 的命令：

1. **后端测试**：
   ```bash
   PYTHONPATH=backend .venv/backend/bin/python -m unittest discover -s backend/tests
   ```
   全部通过（含新增 6 个用例）。

2. **前端构建**：
   ```bash
   cd frontend && npm run build
   ```
   无类型错误。

3. **手动验证**（起服务后）：
   ```bash
   # healthz 增强
   curl -s http://127.0.0.1:18001/healthz | python3 -m json.tool
   # 导出诊断包
   curl -s -X POST "http://127.0.0.1:18001/api/projects/<project_id>/diagnostics/export?max_runs=2" | python3 -m json.tool
   ```
   - 验证返回值含 `bundle_size` / `bundle_size_label`
   - 下载 zip，解压检查：
     - `system_info.json` 内容真实（版本号、OS、bwrap 状态、runtime 列表、provider 配置状态、manager_agent.reachable、worker.active_run_ids）
     - `runs/` 下含动态采集的文件
     - 大文件被 omitted

---

## 文件改动清单（汇总）

| 文件 | 改动类型 | 关键改动 |
|---|---|---|
| `backend/app/services/diagnostic_bundle_service.py` | 大改 | 构造函数 +2 依赖；新增 `_system_info/_backend_version/_os_info/_python_info/_bwrap_info/_provider_info/_probe_manager_agent/_format_size/_prune_old_bundles`；重写 `build_bundle/_manifest/_list_run_file_payload/_write_run_files`；新增常量 |
| `backend/app/services/worker_service.py` | 小改 | 新增 `diagnostic_run_state(project_id)` 公开方法 |
| `backend/app/services/project_service.py` | 小改 | 新增 `get_python_runtimes/get_r_runtimes` 公开代理 |
| `backend/app/api/deps.py` | 小改 | 工厂注入 worker_service + settings（`:221`） |
| `backend/app/main.py` | 小改 | healthz 增强 + `_STARTED_MONOTONIC` + `_backend_version()` |
| `frontend/lib/types.ts` | 小改 | `DiagnosticExportResponse` +2 可选字段 |
| `frontend/components/settings/SettingsPanels.tsx` | 小改 | KV 列表 +文件大小行 |
| `backend/tests/test_diagnostic_bundle.py` | 新建 | 6 个测试用例 |

**不改动**：`backend/app/api/diagnostics.py`（路由本身不变）、`frontend/lib/api.ts`（POST 响应自动带新字段）、manager-agent、schema JSON。

---

## 实施风险与缓解

| 风险 | 缓解 |
|---|---|
| `get_public_settings()` 返回结构与预期不符（键名差异） | 实现时先读 `app_config_service.py:47-92` 核对实际键名；`_provider_info` 按实际结构取值 |
| `_ensure_bwrap_runtime()` 在 bwrap 不可用时抛 RuntimeError | 已用 try/except 捕获，记录 `smoke_ok=False` + error，不中断导出 |
| `_probe_manager_agent` 阻塞 2s 拖慢导出 | 超时设为 2s，且只探活一次；诊断导出非高频，可接受 |
| 测试环境 `Settings()` 构造缺字段 | 参考 `test_worker_service.py` 的实际构造，按需补全 |
| 并发导出 prune 竞争 | 列为已知限制，低频场景暂不处理 |

---

## 不做 / 已知限制（明确边界）

- **不加鉴权**：path-traversal 校验已存在（`diagnostics.py:31-36`）。
- **不改 manager-agent**：已有 `/healthz`。
- **不动 schema 生成**。
- **已知限制**：`_prune_old_bundles` 无并发锁。
- **已知限制**：bwrap smoke 结果有模块级缓存，运行期修复需重启。
- **已知限制**：`_probe_manager_agent` 的 hostname（`os.node`）默认保留不脱敏——若产品要求脱敏，在 `_system_info` 返回前替换。
