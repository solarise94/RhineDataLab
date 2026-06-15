"""Tests for runtime candidate discovery in config.py."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import default_conda_base_candidates, find_conda_solver
from app.services.project_service import ProjectService


class TestDefaultCondaBaseCandidates(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.home_patcher = patch("app.core.config.Path.home", return_value=self.tmp_path)
        self.home_patcher.start()

    def tearDown(self) -> None:
        self.home_patcher.stop()
        self.tmp.cleanup()

    def test_includes_bundled_mamba_path(self) -> None:
        candidates = default_conda_base_candidates()
        self.assertIn(self.tmp_path / ".local/share/blueprint-re/mamba", candidates)

    def test_bundled_path_first_when_configured_base_missing(self) -> None:
        candidates = default_conda_base_candidates()
        self.assertEqual(candidates[0], self.tmp_path / ".local/share/blueprint-re/mamba")

    def test_configured_base_takes_precedence(self) -> None:
        configured = self.tmp_path / "custom-conda"
        candidates = default_conda_base_candidates(configured_base=configured)
        self.assertEqual(candidates[0], configured)


class TestFindCondaSolver(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prefers_micromamba(self) -> None:
        (self.tmp_path / "bin").mkdir()
        (self.tmp_path / "bin" / "micromamba").touch()
        (self.tmp_path / "bin" / "mamba").touch()
        (self.tmp_path / "bin" / "conda").touch()
        self.assertEqual(find_conda_solver(self.tmp_path), self.tmp_path / "bin" / "micromamba")

    def test_falls_back_to_mamba(self) -> None:
        (self.tmp_path / "bin").mkdir()
        (self.tmp_path / "bin" / "mamba").touch()
        (self.tmp_path / "bin" / "conda").touch()
        self.assertEqual(find_conda_solver(self.tmp_path), self.tmp_path / "bin" / "mamba")

    def test_checks_condabin(self) -> None:
        (self.tmp_path / "condabin").mkdir()
        (self.tmp_path / "condabin" / "micromamba").touch()
        self.assertEqual(find_conda_solver(self.tmp_path), self.tmp_path / "condabin" / "micromamba")

    def test_returns_none_when_no_solver(self) -> None:
        (self.tmp_path / "bin").mkdir()
        self.assertIsNone(find_conda_solver(self.tmp_path))


class TestRuntimeSourceClassification(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.home_patcher = patch("app.services.project_service.Path.home", return_value=self.tmp_path)
        self.home_patcher.start()

    def tearDown(self) -> None:
        self.home_patcher.stop()
        self.tmp.cleanup()

    def test_system_source_for_none_path(self) -> None:
        self.assertEqual(ProjectService._runtime_source(None), "system")

    def test_bundled_source_for_bundled_mamba_path(self) -> None:
        bundled = self.tmp_path / ".local/share/blueprint-re/mamba/envs/blueprint-re-r"
        self.assertEqual(ProjectService._runtime_source(bundled), "bundled")

    def test_conda_source_for_user_conda_path(self) -> None:
        conda_env = self.tmp_path / "miniconda3/envs/analysis"
        self.assertEqual(ProjectService._runtime_source(conda_env), "conda")

    def test_system_source_for_unrecognized_path(self) -> None:
        self.assertEqual(ProjectService._runtime_source("/usr/bin/Rscript"), "system")
