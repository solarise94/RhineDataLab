"""Layer C reference-data download integration tests.

Covers:
- source-only reference assets create pending download descriptors
- reference downloads are submitted as dependency jobs
- dedupe keys for reference jobs use source.sha256
- cancellation marks reference jobs failed with error_code="cancelled"
- resolver plan path for reference downloads
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.config import Settings, get_settings
from app.models.card_blueprint import ReferenceDataSourceSpec
from app.services.manager_blueprint_tools import ManagerBlueprintTools
from app.services.project_service import ProjectService
from app.services.reference_data_service import ReferenceDataError, ReferenceDataService
from app.services.runtime_dependency_job_service import RuntimeDependencyJobService
from app.services.runtime_dependency_resolver_service import RuntimeDependencyResolverService
from app.services.runtime_dependency_state_service import (
    compute_dedupe_key,
    find_duplicate_in_flight,
    find_duplicate_terminal_failure,
)


class _TestBase(unittest.TestCase):
    def setUp(self):
        self.data_root = Path(tempfile.mkdtemp(prefix="ref_layer_c_"))
        self.settings = Settings(data_root=self.data_root)
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        shutil.rmtree(self.data_root, ignore_errors=True)

    def _project_service(self):
        with patch("app.services.project_service.get_settings", return_value=self.settings):
            return ProjectService()

    def _make_project(self, project_id: str = "p1"):
        ps = self._project_service()
        ps.create_project(project_id=project_id, name="Test", current_goal="test")
        return ps


class TestRuntimeDependencyStateServiceReferenceDedup(_TestBase):
    def test_compute_dedupe_key_for_reference_uses_sha256(self):
        key = compute_dedupe_key(
            "unknown",
            "unknown",
            [],
            source_sha256="a" * 64,
        )
        self.assertEqual(key, f"ref:{'a' * 64}::")

    def test_compute_dedupe_key_for_install_uses_runtime_and_packages(self):
        key = compute_dedupe_key(
            "python",
            "py3",
            ["numpy", "pandas"],
        )
        self.assertEqual(key, "dep:python:py3:numpy,pandas::")

    def test_find_duplicate_in_flight_reference(self):
        ps = self._make_project("p1")
        project_root = ps.project_path("p1")
        # Write a persisted in-flight reference job.
        (project_root / "chat").mkdir(parents=True, exist_ok=True)
        (project_root / "chat" / "runtime_dependency_jobs.json").write_text(
            '[{"job_id":"j1","project_id":"p1","task_id":"t1","task_type":"reference_data_download",'
            '"payload":{"source":{"spec":{"sha256":"' + "b" * 64 + '","url":"http://x"}}},'
            '"status":"running","phase":"running_subprocess","created_at":"2026-01-01T00:00:00Z"}]'
        )
        dup = find_duplicate_in_flight(
            project_root,
            "",
            "",
            [],
            source_sha256="b" * 64,
        )
        self.assertIsNotNone(dup)
        self.assertEqual(dup["prior_job_id"], "j1")

    def test_cancelled_reference_failure_is_retryable(self):
        ps = self._make_project("p1")
        project_root = ps.project_path("p1")
        (project_root / "chat").mkdir(parents=True, exist_ok=True)
        (project_root / "chat" / "runtime_dependency_jobs.json").write_text(
            '[{"job_id":"j1","project_id":"p1","task_id":"t1","task_type":"reference_data_download",'
            '"payload":{"source":{"spec":{"sha256":"' + "c" * 64 + '","url":"http://x"}}},'
            '"status":"failed","phase":"failed","result":{"ok":false,"error_code":"cancelled"},'
            '"created_at":"2026-01-01T00:00:00Z"}]'
        )
        dup = find_duplicate_terminal_failure(
            project_root,
            "",
            "",
            [],
            source_sha256="c" * 64,
        )
        self.assertIsNone(dup)

    def test_non_retryable_reference_failure_is_cooled(self):
        ps = self._make_project("p1")
        project_root = ps.project_path("p1")
        (project_root / "chat").mkdir(parents=True, exist_ok=True)
        (project_root / "chat" / "runtime_dependency_jobs.json").write_text(
            '[{"job_id":"j1","project_id":"p1","task_id":"t1","task_type":"reference_data_download",'
            '"payload":{"source":{"spec":{"sha256":"' + "d" * 64 + '","url":"http://x"}}},'
            '"status":"failed","phase":"failed","result":{"ok":false,"error_code":"external_source_install_not_supported"},'
            '"created_at":"2026-01-01T00:00:00Z"}]'
        )
        dup = find_duplicate_terminal_failure(
            project_root,
            "",
            "",
            [],
            source_sha256="d" * 64,
        )
        self.assertIsNotNone(dup)
        self.assertEqual(dup["prior_status"], "failed")


class TestRuntimeDependencyJobServiceReference(_TestBase):
    def _service(self, ps, max_workers=4):
        svc = RuntimeDependencyJobService(
            ps,
            max_workers=max_workers,
        )
        return svc

    def test_reference_download_job_runs_handler(self):
        ps = self._make_project("p1")
        called = {}

        def handler(project_id, payload, phase_callback=None, *, job_id=None, cancel_event=None):
            called["payload"] = payload
            called["job_id"] = job_id
            called["cancel_event"] = cancel_event
            return {"ok": True, "ref_id": "r1"}

        svc = self._service(ps)
        try:
            job = svc.submit(
                project_id="p1",
                payload={"source": {"sha256": "e" * 64, "url": "http://x", "spec": {"sha256": "e" * 64, "url": "http://x"}}},
                handler=handler,
                task_type="reference_data_download",
            )
            deadline = time.time() + 5
            while svc.get(job.job_id).status != "succeeded" and time.time() < deadline:
                time.sleep(0.05)
            job = svc.get(job.job_id)
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(called["payload"]["source"]["spec"]["sha256"], "e" * 64)
            self.assertEqual(called["job_id"], job.job_id)
            self.assertIsInstance(called["cancel_event"], threading.Event)
        finally:
            svc.executor.shutdown(wait=True)

    def test_cancel_marks_job_failed_with_cancelled_error_code(self):
        ps = self._make_project("p1")
        started = threading.Event()

        def handler(project_id, payload, phase_callback=None, *, job_id=None, cancel_event=None):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.01)
            return {"ok": False, "error_code": "cancelled", "message": "cancelled"}

        svc = self._service(ps)
        try:
            job = svc.submit(
                project_id="p1",
                payload={"source": {"sha256": "f" * 64, "url": "http://x", "spec": {"sha256": "f" * 64, "url": "http://x"}}},
                handler=handler,
                task_type="reference_data_download",
            )
            self.assertTrue(started.wait(timeout=5))
            svc.cancel("p1", job.job_id)
            deadline = time.time() + 5
            while svc.get(job.job_id).status != "failed" and time.time() < deadline:
                time.sleep(0.05)
            job = svc.get(job.job_id)
            self.assertEqual(job.status, "failed")
            self.assertIsNotNone(job.result)
            self.assertEqual(job.result.get("error_code"), "cancelled")
        finally:
            svc.executor.shutdown(wait=True)

    def test_default_worker_pool_size_allows_two_install_plus_one_download(self):
        ps = self._make_project("p1")
        svc = RuntimeDependencyJobService(ps)
        try:
            self.assertEqual(svc.executor._max_workers, 3)
        finally:
            svc.executor.shutdown(wait=True)

    def test_two_install_plus_one_download_run_concurrently(self):
        ps = self._make_project("p1")
        release_event = threading.Event()
        started_lock = threading.Lock()
        started_count = 0

        def make_handler(tag):
            def handler(*args, **kwargs):
                nonlocal started_count
                with started_lock:
                    started_count += 1
                self.assertTrue(release_event.wait(timeout=10), f"{tag} timed out waiting for release")
                return {"ok": True}
            return handler

        svc = RuntimeDependencyJobService(ps)
        try:
            job1 = svc.submit(
                "p1",
                {"runtime": "python_env", "packages": ["x"]},
                handler=make_handler("install1"),
            )
            job2 = svc.submit(
                "p1",
                {"runtime": "r_env", "packages": ["y"]},
                handler=make_handler("install2"),
            )
            job3 = svc.submit(
                "p1",
                {"source": {"sha256": "h" * 64, "url": "http://x", "spec": {"sha256": "h" * 64, "url": "http://x"}}},
                handler=make_handler("download"),
                task_type="reference_data_download",
            )
            deadline = time.time() + 10
            while started_count < 3 and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(started_count, 3, "Expected all three handlers to be running concurrently")
            release_event.set()
            for job in (job1, job2, job3):
                job.future.result(timeout=5)
        finally:
            svc.executor.shutdown(wait=True)

    def test_cancel_before_handler_start_reuses_cancel_event(self):
        ps = self._make_project("p1")
        release_dummy = threading.Event()
        saw_cancelled_at_start = []

        def dummy_handler(*args, **kwargs):
            release_dummy.wait(timeout=10)
            return {"ok": True}

        def real_handler(project_id, payload, phase_callback=None, *, job_id=None, cancel_event=None):
            saw_cancelled_at_start.append(cancel_event.is_set())
            return {"ok": False, "error_code": "cancelled", "message": "cancelled"}

        svc = self._service(ps, max_workers=1)
        try:
            dummy_job = svc.submit("p1", {"runtime": "dummy", "packages": ["x"]}, handler=dummy_handler)
            real_job = svc.submit(
                "p1",
                {"source": {"sha256": "g" * 64, "url": "http://x", "spec": {"sha256": "g" * 64, "url": "http://x"}}},
                handler=real_handler,
                task_type="reference_data_download",
            )
            svc.cancel("p1", real_job.job_id)
            release_dummy.set()
            real_job.future.result(timeout=5)
            job = svc.get(real_job.job_id)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.result.get("error_code"), "cancelled")
            self.assertEqual(saw_cancelled_at_start, [True])
        finally:
            svc.executor.shutdown(wait=True)


class TestManagerBlueprintToolsReferenceHandler(_TestBase):
    def setUp(self):
        super().setUp()
        self.ps = self._make_project("p1")
        self.tools = ManagerBlueprintTools(
            project_service=self.ps,
        )
        self.src = ReferenceDataSourceSpec(
            url="http://127.0.0.1:0/data.txt",
            sha256="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            kind="other",
        )

    def test_resolve_reference_download_plan_reports_download_action(self):
        resolver = RuntimeDependencyResolverService()
        tools = ManagerBlueprintTools(
            project_service=self.ps,
            runtime_dependency_resolver_service=resolver,
        )
        plan = tools.resolve_runtime_dependencies(
            "p1",
            {"source": {"spec": self.src.model_dump()}, "role": "genome"},
        )
        self.assertTrue(plan.get("ok"))
        self.assertTrue(plan.get("background"))
        self.assertEqual(plan.get("request_dedupe_key"), f"ref:{self.src.sha256}")
        self.assertEqual(plan["descriptor"]["role"], "genome")

    @patch.object(ReferenceDataService, "fetch_and_register")
    def test_download_reference_asset_handler_calls_fetch_and_register(self, mock_fetch):
        mock_meta = MagicMock()
        mock_meta.ref_id = "r1"
        mock_meta.sha256 = self.src.sha256
        mock_meta.kind = "text"
        mock_fetch.return_value = mock_meta
        result = self.tools._download_reference_asset_sync(
            "p1",
            {"source": {"spec": self.src.model_dump()}, "role": "genome"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["sha256"], self.src.sha256)
        mock_fetch.assert_called_once()

    def test_download_reference_asset_handler_rejects_missing_sha256(self):
        result = self.tools._download_reference_asset_sync(
            "p1",
            {"source": {"spec": {"url": "http://x"}}, "role": "genome"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_reference_payload")

    @patch.object(ReferenceDataService, "fetch_and_register")
    @patch.object(ReferenceDataService, "resolve")
    def test_download_reference_asset_handler_updates_card_executor_context(
        self, mock_resolve, mock_fetch
    ):
        from app.models.cards import Card
        from app.models.executor import ExecutorContext

        card_id = "card-with-ref"
        store = self.ps.graph_store("p1")
        store.save_cards([
            Card(
                card_id=card_id,
                card_type="module",
                title="Reference card",
                status="proposed",
                summary="test",
                executor_context=ExecutorContext(),
            ),
        ])

        mock_meta = MagicMock()
        mock_meta.ref_id = "r1"
        mock_meta.sha256 = self.src.sha256
        mock_meta.kind = "text"
        mock_meta.size = 123
        mock_fetch.return_value = mock_meta

        expected_path = self.data_root / "_system" / "reference-data" / "r1" / "data" / "data.txt"
        mock_resolve.return_value = expected_path

        job_svc = RuntimeDependencyJobService(self.ps, max_workers=2)
        tools = ManagerBlueprintTools(
            project_service=self.ps,
            runtime_dependency_job_service=job_svc,
        )
        try:
            job = job_svc.submit(
                project_id="p1",
                payload={
                    "source": {
                        "card_id": card_id,
                        "spec": self.src.model_dump(),
                    },
                    "role": "genome",
                },
                handler=tools._download_reference_asset_sync,
                task_type="reference_data_download",
            )
            deadline = time.time() + 5
            while job_svc.get(job.job_id).status != "succeeded" and time.time() < deadline:
                time.sleep(0.05)
            job = job_svc.get(job.job_id)
            self.assertEqual(job.status, "succeeded")
        finally:
            job_svc.executor.shutdown(wait=True)

        cards = store.load_cards()
        card = next(c for c in cards if c.card_id == card_id)
        self.assertIsNotNone(card.executor_context)
        self.assertIn(
            str(expected_path),
            card.executor_context.template_metadata.get("reference_paths", {}).get("genome", []),
        )
        references = card.executor_context.references
        self.assertTrue(any(
            r.type == "file" and r.description == "genome" and r.path == str(expected_path)
            for r in references
        ))


class TestReferenceDataServiceDedup(_TestBase):
    def test_second_fetch_with_same_sha256_skips_network_io(self):
        svc = ReferenceDataService(self.settings)
        content = b"hello dedup"
        tmp = Path(tempfile.mktemp(dir=self.data_root))
        tmp.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()

        source = ReferenceDataSourceSpec(
            url="http://example.com/ref.bin",
            sha256=digest,
            kind="other",
        )

        calls = []

        def fake_stream_download(src, url, **kwargs):
            calls.append(url)
            return tmp, digest

        with patch.object(svc, "_stream_download", side_effect=fake_stream_download):
            meta1 = svc.fetch_and_register(source)
            self.assertEqual(len(calls), 1)
            self.assertEqual(meta1.sha256, digest)

            meta2 = svc.fetch_and_register(source)
            self.assertEqual(len(calls), 1)  # no additional network call
            self.assertEqual(meta2.ref_id, meta1.ref_id)
            self.assertEqual(meta2.sha256, meta1.sha256)


class TestReferenceDataServiceCancellation(_TestBase):
    def test_stream_download_respects_cancel_event(self):
        svc = ReferenceDataService(self.settings)
        cancel_event = threading.Event()
        cancel_event.set()

        class _FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b""

        fake_opener = MagicMock()
        fake_opener.open.return_value = _FakeResponse()

        source = ReferenceDataSourceSpec(
            url="http://127.0.0.1:1/nope",
            sha256="a" * 64,
        )
        with patch("urllib.request.build_opener", return_value=fake_opener):
            with self.assertRaises(ReferenceDataError) as ctx:
                svc._stream_download(source, "http://127.0.0.1:1/nope", cancel_event=cancel_event)
        self.assertIn("cancelled", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
