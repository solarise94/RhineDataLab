from __future__ import annotations

import unittest

from app.services.runtime_dependency_state_service import (
    _tail_text_bounded,
    compute_dedupe_key,
    _normalize_packages_for_key,
    _retry_hint_for_error_code,
    runtime_dependency_failure_details,
    find_duplicate_in_flight,
    find_duplicate_terminal_failure,
    dependency_blockers_by_card,
)
from app.services.utils import utc_now


class RuntimeDependencyStateServiceTest(unittest.TestCase):
    def test_tail_text_bounded_short_text_unchanged(self):
        text = "short"
        self.assertEqual(_tail_text_bounded(text), "short")

    def test_tail_text_bounded_truncates_at_lines(self):
        text = "\n".join(f"line {i}" for i in range(60))
        result = _tail_text_bounded(text, max_lines=50)
        self.assertEqual(result.count("\n"), 49)

    def test_tail_text_bounded_truncates_at_bytes(self):
        text = "a" * 3000
        result = _tail_text_bounded(text, max_bytes=2048)
        self.assertLessEqual(len(result.encode("utf-8")), 2048)

    def test_tail_text_bounded_unicode_safe(self):
        text = "日本語" * 500
        result = _tail_text_bounded(text, max_bytes=100)
        # Should not raise and should be valid UTF-8
        result.encode("utf-8")

    def test_normalize_packages_for_key_dedupes(self):
        packages = ["numpy", "Numpy", " pandas ", "numpy"]
        result = _normalize_packages_for_key(packages, "python")
        self.assertEqual(result, ["numpy", "pandas"])

    def test_normalize_packages_for_key_preserves_order(self):
        packages = ["b", "a", "b", "c"]
        result = _normalize_packages_for_key(packages, "python")
        self.assertEqual(result, ["b", "a", "c"])

    def test_compute_dedupe_key_sorts_packages(self):
        key1 = compute_dedupe_key("python", "env1", ["b", "a"])
        key2 = compute_dedupe_key("python", "env1", ["a", "b"])
        self.assertEqual(key1, key2)
        self.assertEqual(key1, "dep:python:env1:a,b::")

    def test_compute_dedupe_key_includes_error_and_package(self):
        key = compute_dedupe_key(
            "python", "env1", ["numpy"],
            error_code="package_not_found_in_conda_channels",
            requested_package="numpy",
        )
        self.assertEqual(key, "dep:python:env1:numpy:package_not_found_in_conda_channels:numpy")

    def test_retry_hint_mapping(self):
        self.assertEqual(
            _retry_hint_for_error_code("package_not_found_in_conda_channels"),
            "do_not_retry_same_conda_request",
        )
        self.assertEqual(
            _retry_hint_for_error_code("github_source_install_not_supported"),
            "do_not_retry_installer",
        )
        self.assertEqual(
            _retry_hint_for_error_code("dependency_install_timeout"),
            "retry_allowed_after_runtime_check",
        )
        self.assertEqual(
            _retry_hint_for_error_code("dependency_install_start_failed"),
            "manual_runtime_preparation_required",
        )
        self.assertEqual(
            _retry_hint_for_error_code("dependency_install_compilation_failed"),
            "manual_system_dependency_or_runtime_preparation_required",
        )
        self.assertEqual(
            _retry_hint_for_error_code("dependency_install_failed"),
            "inspect_stderr",
        )
        self.assertIsNone(_retry_hint_for_error_code(None))
        self.assertIsNone(_retry_hint_for_error_code("unknown_code"))

    def test_failure_details_from_persisted_dict(self):
        job = {
            "job_id": "depjob_abc123",
            "task_id": "bgtask_abc123",
            "status": "failed",
            "payload": {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["pydeseq2"],
                "source": {"card_id": "card_1", "run_id": "run_1", "session_id": "session_1"},
            },
            "result": {
                "ok": False,
                "error_code": "package_not_found_in_conda_channels",
                "requested_package": "pydeseq2",
                "attempted_candidates": ["pydeseq2"],
                "fallback_available": ["pip"],
                "message": "Package pydeseq2 was not found in conda channels.",
                "stdout_tail": "",
                "stderr_tail": "error",
            },
            "error": "",
            "created_at": "2026-06-01T00:00:00Z",
            "started_at": "2026-06-01T00:01:00Z",
            "finished_at": "2026-06-01T00:02:00Z",
        }
        details = runtime_dependency_failure_details(job)
        self.assertEqual(details["job_id"], "depjob_abc123")
        self.assertEqual(details["status"], "failed")
        self.assertEqual(details["ecosystem"], "python")
        self.assertEqual(details["runtime"], "python_env")
        self.assertEqual(details["packages"], ["pydeseq2"])
        self.assertEqual(details["card_id"], "card_1")
        self.assertEqual(details["run_id"], "run_1")
        self.assertEqual(details["session_id"], "session_1")
        self.assertEqual(details["error_code"], "package_not_found_in_conda_channels")
        self.assertEqual(details["requested_package"], "pydeseq2")
        self.assertEqual(details["attempted_candidates"], ["pydeseq2"])
        self.assertEqual(details["fallback_available"], ["pip"])
        self.assertEqual(details["retry_hint"], "do_not_retry_same_conda_request")
        self.assertEqual(details["ok"], False)
        self.assertIn("dedupe_key", details)

    def test_failure_details_reference_job_uses_ref_dedupe_key(self):
        sha256 = "a" * 64
        job = {
            "job_id": "refjob_abc123",
            "task_id": "bgtask_abc123",
            "status": "failed",
            "payload": {
                "source": {
                    "card_id": "card_1",
                    "spec": {
                        "sha256": sha256,
                        "url": "http://example.com/data.txt",
                    },
                },
                "role": "genome",
            },
            "result": {
                "ok": False,
                "error_code": "external_source_install_not_supported",
                "message": "Download failed.",
            },
            "created_at": "2026-06-01T00:00:00Z",
        }
        details = runtime_dependency_failure_details(job)
        self.assertEqual(details["job_id"], "refjob_abc123")
        self.assertEqual(details["card_id"], "card_1")
        self.assertEqual(details["error_code"], "external_source_install_not_supported")
        self.assertEqual(details["dedupe_key"], f"ref:{sha256}:external_source_install_not_supported:")

    def test_failure_details_strips_none_values(self):
        job = {
            "job_id": "depjob_abc123",
            "task_id": "bgtask_abc123",
            "status": "succeeded",
            "payload": {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["numpy"],
            },
            "result": {"ok": True},
            "created_at": "2026-06-01T00:00:00Z",
        }
        details = runtime_dependency_failure_details(job)
        self.assertNotIn("error_code", details)
        self.assertNotIn("message", details)
        self.assertNotIn("requested_package", details)
        self.assertEqual(details["ok"], True)

    def test_dependency_blockers_skips_manually_resolved(self):
        from pathlib import Path
        import tempfile
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            chat_dir = project_root / "chat"
            chat_dir.mkdir(parents=True, exist_ok=True)
            jobs = [
                {
                    "job_id": "depjob_1",
                    "status": "failed",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "runtime": "python_env",
                        "packages": ["numpy"],
                        "source": {"card_id": "card_1"},
                        "resolution_status": "manually_resolved",
                        "resolved_at": "2026-06-01T01:00:00Z",
                    },
                    "result": {"ok": False, "error_code": "package_not_found_in_conda_channels"},
                }
            ]
            (chat_dir / "runtime_dependency_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
            blockers = dependency_blockers_by_card(project_root)
            self.assertNotIn("card_1", blockers)

    def test_dependency_blockers_includes_unresolved_failed(self):
        from pathlib import Path
        import tempfile
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            chat_dir = project_root / "chat"
            chat_dir.mkdir(parents=True, exist_ok=True)
            jobs = [
                {
                    "job_id": "depjob_1",
                    "status": "failed",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "runtime": "python_env",
                        "packages": ["numpy"],
                        "source": {"card_id": "card_1"},
                    },
                    "result": {"ok": False, "error_code": "package_not_found_in_conda_channels"},
                }
            ]
            (chat_dir / "runtime_dependency_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
            blockers = dependency_blockers_by_card(project_root)
            self.assertIn("card_1", blockers)
            self.assertEqual(blockers["card_1"]["error_code"], "package_not_found_in_conda_channels")
            self.assertEqual(blockers["card_1"]["retry_hint"], "do_not_retry_same_conda_request")


class RuntimeDependencyDuplicateLookupTest(unittest.TestCase):
    def _write_jobs(self, project_root, jobs):
        import json
        chat_dir = project_root / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        (chat_dir / "runtime_dependency_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")

    def test_find_duplicate_in_flight_matching(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            self._write_jobs(project_root, [
                {
                    "job_id": "depjob_1",
                    "status": "running",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "ecosystem": "python",
                        "runtime": "python_env",
                        "packages": ["numpy"],
                    },
                }
            ])
            dup = find_duplicate_in_flight(project_root, "python", "python_env", ["numpy"])
            self.assertIsNotNone(dup)
            self.assertEqual(dup["prior_job_id"], "depjob_1")

    def test_find_duplicate_in_flight_different_runtime(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            self._write_jobs(project_root, [
                {
                    "job_id": "depjob_1",
                    "status": "running",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "ecosystem": "python",
                        "runtime": "python_env",
                        "packages": ["numpy"],
                    },
                }
            ])
            dup = find_duplicate_in_flight(project_root, "python", "other_env", ["numpy"])
            self.assertIsNone(dup)

    def test_find_duplicate_terminal_failure_cools_non_retryable(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            self._write_jobs(project_root, [
                {
                    "job_id": "depjob_1",
                    "status": "failed",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "ecosystem": "python",
                        "runtime": "python_env",
                        "packages": ["numpy"],
                    },
                    "result": {
                        "ok": False,
                        "error_code": "package_not_found_in_conda_channels",
                        "requested_package": "numpy",
                        "fallback_available": ["pip"],
                    },
                }
            ])
            dup = find_duplicate_terminal_failure(project_root, "python", "python_env", ["numpy"])
            self.assertIsNotNone(dup)
            self.assertEqual(dup["prior_job_id"], "depjob_1")
            self.assertEqual(dup["prior_error_code"], "package_not_found_in_conda_channels")

    def test_find_duplicate_terminal_failure_does_not_cool_timeout(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            self._write_jobs(project_root, [
                {
                    "job_id": "depjob_1",
                    "status": "failed",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "ecosystem": "python",
                        "runtime": "python_env",
                        "packages": ["numpy"],
                    },
                    "result": {
                        "ok": False,
                        "error_code": "dependency_install_timeout",
                    },
                }
            ])
            dup = find_duplicate_terminal_failure(project_root, "python", "python_env", ["numpy"])
            self.assertIsNone(dup)

    def test_find_duplicate_terminal_failure_different_packages(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            self._write_jobs(project_root, [
                {
                    "job_id": "depjob_1",
                    "status": "failed",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "ecosystem": "python",
                        "runtime": "python_env",
                        "packages": ["numpy"],
                    },
                    "result": {
                        "ok": False,
                        "error_code": "package_not_found_in_conda_channels",
                    },
                }
            ])
            dup = find_duplicate_terminal_failure(project_root, "python", "python_env", ["pandas"])
            self.assertIsNone(dup)

    def test_backend_restart_preserves_terminal_cooling(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            self._write_jobs(project_root, [
                {
                    "job_id": "depjob_1",
                    "status": "failed",
                    "created_at": "2026-06-01T00:00:00Z",
                    "payload": {
                        "ecosystem": "python",
                        "runtime": "python_env",
                        "packages": ["numpy"],
                    },
                    "result": {
                        "ok": False,
                        "error_code": "package_not_found_in_conda_channels",
                    },
                }
            ])
            # Simulate no in-memory state by using a fresh lookup
            dup = find_duplicate_terminal_failure(project_root, "python", "python_env", ["numpy"])
            self.assertIsNotNone(dup)
            self.assertEqual(dup["prior_job_id"], "depjob_1")


if __name__ == "__main__":
    unittest.main()
