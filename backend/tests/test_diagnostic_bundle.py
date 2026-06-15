"""Tests for DiagnosticBundleService."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.core.config import get_settings
from app.models.graph import RunRecord
from app.services.app_config_service import AppConfigService
from app.services.diagnostic_bundle_service import DiagnosticBundleService
from app.services.manifest_service import ManifestService
from app.services.project_service import ProjectService
from app.services.runtime_approval_service import RuntimeApprovalService
from app.services.worker_service import WorkerService


class DiagnosticBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="diag-test-"))
        get_settings.cache_clear()
        # ProjectService reads from the cached global settings instance.
        self.settings = get_settings()
        self.settings.data_root = self.tmpdir
        self.settings.executor_sandbox_mode = "none"

        self.project_service = ProjectService()
        self.app_config_service = AppConfigService(self.settings)
        self.worker_service = WorkerService(
            self.project_service,
            ManifestService(self.project_service),
            RuntimeApprovalService(self.project_service),
        )
        self.service = DiagnosticBundleService(
            self.project_service,
            self.app_config_service,
            self.worker_service,
            self.settings,
        )

        self.project_id = "diag-test"
        self.project_service.create_project(project_id=self.project_id, name="Diag Test", current_goal="test")

    def tearDown(self) -> None:
        # Clear cached factories so later tests that use FastAPI deps start clean.
        from app.api.deps import (
            get_app_config_service,
            get_diagnostic_bundle_service,
            get_project_service,
            get_worker_service,
        )

        get_settings.cache_clear()
        get_project_service.cache_clear()
        get_app_config_service.cache_clear()
        get_worker_service.cache_clear()
        get_diagnostic_bundle_service.cache_clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_run(self, run_id: str, status: str = "success") -> None:
        store = self.project_service.graph_store(self.project_id)
        runs = store.load_runs()
        runs.append(
            RunRecord(
                run_id=run_id,
                card_id="card-1",
                status=status,  # type: ignore[arg-type]
                title="Test run",
                summary="",
                started_at="2026-06-15T10:00:00Z",
                finished_at="2026-06-15T10:01:00Z",
            )
        )
        store.save_runs(runs)

    def _extract_bundle(self, bundle_path: Path) -> Path:
        extract_dir = Path(tempfile.mkdtemp(prefix="diag-extract-"))
        self.addCleanup(shutil.rmtree, extract_dir, ignore_errors=True)
        with zipfile.ZipFile(bundle_path, "r") as archive:
            archive.extractall(extract_dir)
        return extract_dir / f"{self.project_id}_diagnostic_bundle"

    def test_bundle_contains_system_info(self) -> None:
        self._add_run("run-1")
        result = self.service.build_bundle(self.project_id, max_runs=2)
        bundle_path = self.project_service.project_path(self.project_id) / result["path"]
        root = self._extract_bundle(bundle_path)
        system_info_path = root / "system_info.json"
        self.assertTrue(system_info_path.exists())

        system_info = json.loads(system_info_path.read_text(encoding="utf-8"))
        for key in (
            "backend_version",
            "os",
            "python",
            "bwrap",
            "python_runtimes",
            "r_runtimes",
            "providers",
            "manager_agent",
            "worker",
        ):
            self.assertIn(key, system_info)
        self.assertIn("reachable", system_info["manager_agent"])
        self.assertIn("active_run_ids", system_info["worker"])
        self.assertIn("stuck_run_ids", system_info["worker"])

    def test_dynamic_run_file_collection(self) -> None:
        self._add_run("run-1")
        run_dir = self.project_service.project_path(self.project_id) / "runs" / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "custom_trace.json").write_text('{"trace": true}', encoding="utf-8")
        (run_dir / "artifact.bin").write_text("binary", encoding="utf-8")

        result = self.service.build_bundle(self.project_id, max_runs=2)
        bundle_path = self.project_service.project_path(self.project_id) / result["path"]
        root = self._extract_bundle(bundle_path)
        self.assertTrue((root / "runs" / "run-1" / "custom_trace.json").exists())
        self.assertFalse((root / "runs" / "run-1" / "artifact.bin").exists())

    def test_large_run_file_omitted(self) -> None:
        self._add_run("run-1")
        run_dir = self.project_service.project_path(self.project_id) / "runs" / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        large_file = run_dir / "big.json"
        large_file.write_bytes(b"0" * (3 * 1024 * 1024))

        result = self.service.build_bundle(self.project_id, max_runs=2)
        bundle_path = self.project_service.project_path(self.project_id) / result["path"]
        root = self._extract_bundle(bundle_path)
        self.assertFalse((root / "runs" / "run-1" / "big.json").exists())

        meta = json.loads((root / "runs" / "run-1" / "_meta.json").read_text(encoding="utf-8"))
        self.assertIn("omitted_files", meta)
        omitted = [item["path"] for item in meta["omitted_files"]]
        self.assertTrue(any("big.json" in p for p in omitted))

    def test_bundle_prune(self) -> None:
        self._add_run("run-1")
        for _ in range(12):
            self.service.build_bundle(self.project_id, max_runs=2)
        bundle_dir = self.project_service.project_path(self.project_id) / "reports" / "diagnostics"
        bundles = list(bundle_dir.glob("*.zip"))
        self.assertEqual(len(bundles), 10)

    def test_download_url_encoded(self) -> None:
        self._add_run("run-1")
        result = self.service.build_bundle(self.project_id, max_runs=2)
        download_url = result["download_url"]
        self.assertIn("?path=", download_url)
        # The path contains underscores and slashes only; ensure no unencoded spaces/quotes.
        from urllib.parse import urlparse, unquote

        parsed = urlparse(download_url)
        query = parsed.query
        self.assertIn("path=", query)
        path_value = unquote(query.split("path=", 1)[1])
        self.assertEqual(path_value, result["path"])

    def test_session_count_single_load(self) -> None:
        self._add_run("run-1")
        with patch.object(self.service, "_chat_sessions", wraps=self.service._chat_sessions) as mock_sessions:
            self.service.build_bundle(self.project_id, max_runs=2)
            self.assertEqual(mock_sessions.call_count, 1)


if __name__ == "__main__":
    unittest.main()
