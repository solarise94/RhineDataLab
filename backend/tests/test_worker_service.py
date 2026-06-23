"""Tests for WorkerService context construction and command worker env injection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class TestBwrapRuntimeSelection(_Base):
    def tearDown(self):
        from app.workers.sandbox import bwrap

        bwrap._BWRAP_SMOKE_CACHE.clear()
        super().tearDown()

    def test_ensure_bwrap_runtime_prefers_configured_env_var(self):
        from app.workers.sandbox import bwrap

        bwrap._BWRAP_SMOKE_CACHE.clear()
        with patch.dict(os.environ, {"BLUEPRINT_BWRAP_BIN": "/usr/bin/bwrap"}, clear=False), patch(
            "app.workers.sandbox.bwrap.shutil.which",
            side_effect=lambda name: "/usr/bin/fallback-bwrap" if name == "bwrap" else None,
        ), patch(
            "app.workers.sandbox.bwrap.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as run_mock:
            resolved = bwrap.ensure_bwrap_runtime()

        self.assertEqual(resolved, "/usr/bin/bwrap")
        self.assertEqual(run_mock.call_args.args[0][0], "/usr/bin/bwrap")


class TestSandboxModeValidation(_Base):
    def test_settings_sandbox_mode_rejects_unknown_mode(self):
        from app.workers.sandbox import settings_sandbox_mode

        settings = Settings(data_root=self.data_root, executor_sandbox_mode="unknown_mode")
        with self.assertRaises(RuntimeError) as ctx:
            settings_sandbox_mode(settings)
        self.assertIn("unknown_mode", str(ctx.exception))
        self.assertIn("bwrap, container, seatbelt, none", str(ctx.exception))

    def test_resolve_renderer_rejects_unknown_mode(self):
        from app.workers.sandbox import resolve_renderer

        with self.assertRaises(RuntimeError) as ctx:
            resolve_renderer("unknown_mode")
        self.assertIn("unknown_mode", str(ctx.exception))

    def test_adapter_uses_sandbox_rejects_unknown_mode(self):
        adapter = ShellWorkerAdapter()
        settings = Settings(data_root=self.data_root, executor_sandbox_mode="unknown_mode")
        with self.assertRaises(RuntimeError) as ctx:
            adapter.uses_sandbox(settings)
        self.assertIn("unknown_mode", str(ctx.exception))


class TestNoneRenderer(_Base):
    def test_resolve_renderer_none_returns_no_op_renderer(self):
        from app.workers.sandbox import NoneRenderer, resolve_renderer

        renderer = resolve_renderer("none")
        self.assertIsInstance(renderer, NoneRenderer)
        self.assertEqual(renderer.mode, "none")
        self.assertFalse(renderer.should_sandbox())

    def test_none_renderer_leaves_command_unchanged(self):
        from app.workers.sandbox import resolve_renderer

        renderer = resolve_renderer("none")
        command = ["python", "script.py"]
        result, plan = renderer.render(
            command=command,
            packet=None,
            project_root=self.data_root,
            run_dir=self.data_root / "runs" / "run-1",
            environment={},
            adapter_extra_env_keys=set(),
            settings=self.settings,
        )
        self.assertEqual(result, command)
        self.assertEqual(plan, {"mode": "none"})

    def test_adapter_uses_sandbox_false_for_none_mode(self):
        adapter = ShellWorkerAdapter()
        settings = Settings(data_root=self.data_root, executor_sandbox_mode="none")
        self.assertFalse(adapter.uses_sandbox(settings))


if __name__ == "__main__":
    unittest.main()
