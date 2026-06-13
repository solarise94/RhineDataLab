"""Tests for WorkerService context construction and command worker env injection."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings, get_settings
from app.models.executor import ExecutorContext, ExecutorReference, RuntimeBindings
from app.models.runs import ExecutionPolicy, RunContext, TaskPacket
from app.workers.shell_worker import ShellWorkerAdapter


class _Base(unittest.TestCase):
    def setUp(self):
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root, executor_sandbox_mode="none")
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        shutil.rmtree(self.data_root, ignore_errors=True)


class TestMergeExecutorContext(_Base):
    def test_merge_template_metadata_preserves_defaults(self):
        from app.services.worker_service import WorkerService

        default = ExecutorContext(
            template_metadata={"default_key": "default_value"},
        )
        override = ExecutorContext(
            template_metadata={"reference_paths": {"gene_annotation": "/data/genes.gtf"}},
        )
        merged = WorkerService._merge_executor_context(default, override)
        self.assertEqual(merged.template_metadata["default_key"], "default_value")
        self.assertEqual(
            merged.template_metadata["reference_paths"],
            {"gene_annotation": "/data/genes.gtf"},
        )


class TestCommandWorkerReferencePaths(_Base):
    def _packet(self, executor_context: ExecutorContext | None) -> TaskPacket:
        return TaskPacket(
            task_id="task-1",
            project_id="proj-1",
            card_id="card-1",
            goal="test",
            worker_instructions="test",
            run_context=RunContext(
                run_id="run-1",
                worker_type="shell",
                project_root=str(self.data_root),
                run_dir=str(self.data_root / "runs" / "run-1"),
                result_dir=str(self.data_root / "results" / "card-1" / "run-1"),
            ),
            executor_context=executor_context,
            execution_policy=ExecutionPolicy(mode="audit"),
        )

    def test_reference_paths_env_ignores_default_params_yaml(self):
        adapter = ShellWorkerAdapter()
        packet = self._packet(
            ExecutorContext(
                references=[
                    ExecutorReference(type="file", path="configs/params.yaml", description="Project-level runtime parameters."),
                    ExecutorReference(type="file", path="/data/genes.gtf", description="gene_annotation"),
                ],
                runtime_bindings=RuntimeBindings(),
                template_metadata={"reference_paths": {"gene_annotation": "/data/genes.gtf"}},
            )
        )
        run_dir = self.data_root / "runs" / "run-1"
        packet_path = run_dir / "packet.json"
        run_dir.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(packet.model_dump_json())

        spec = adapter.build_launch_spec(
            packet=packet,
            packet_path=packet_path,
            run_dir=run_dir,
            project_root=self.data_root,
            settings=self.settings,
        )
        reference_paths = json.loads(spec.environment["BLUEPRINT_REFERENCE_PATHS"])
        self.assertEqual(reference_paths, {"gene_annotation": "/data/genes.gtf"})
        self.assertNotIn("Project-level runtime parameters.", reference_paths)

    def test_reference_paths_env_defaults_to_empty_dict(self):
        adapter = ShellWorkerAdapter()
        packet = self._packet(
            ExecutorContext(
                references=[
                    ExecutorReference(type="file", path="configs/params.yaml", description="Project-level runtime parameters."),
                ],
                runtime_bindings=RuntimeBindings(),
            )
        )
        run_dir = self.data_root / "runs" / "run-1"
        packet_path = run_dir / "packet.json"
        run_dir.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(packet.model_dump_json())

        spec = adapter.build_launch_spec(
            packet=packet,
            packet_path=packet_path,
            run_dir=run_dir,
            project_root=self.data_root,
            settings=self.settings,
        )
        reference_paths = json.loads(spec.environment["BLUEPRINT_REFERENCE_PATHS"])
        self.assertEqual(reference_paths, {})


if __name__ == "__main__":
    unittest.main()
