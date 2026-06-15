"""Tests for runtime candidate discovery in config.py."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import default_conda_base_candidates, find_conda_solver
from app.services.project_service import ProjectService


class TestDefaultCondaBaseCandidates:
    def test_includes_bundled_mamba_path(self, tmp_path: Path) -> None:
        with patch("app.core.config.Path.home", return_value=tmp_path):
            candidates = default_conda_base_candidates()
        assert tmp_path / ".local/share/blueprint-re/mamba" in candidates

    def test_bundled_path_first_when_configured_base_missing(self, tmp_path: Path) -> None:
        with patch("app.core.config.Path.home", return_value=tmp_path):
            candidates = default_conda_base_candidates()
        assert candidates[0] == tmp_path / ".local/share/blueprint-re/mamba"

    def test_configured_base_takes_precedence(self, tmp_path: Path) -> None:
        configured = tmp_path / "custom-conda"
        with patch("app.core.config.Path.home", return_value=tmp_path):
            candidates = default_conda_base_candidates(configured_base=configured)
        assert candidates[0] == configured


class TestFindCondaSolver:
    def test_prefers_micromamba(self, tmp_path: Path) -> None:
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "micromamba").touch()
        (tmp_path / "bin" / "mamba").touch()
        (tmp_path / "bin" / "conda").touch()
        assert find_conda_solver(tmp_path) == tmp_path / "bin" / "micromamba"

    def test_falls_back_to_mamba(self, tmp_path: Path) -> None:
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "mamba").touch()
        (tmp_path / "bin" / "conda").touch()
        assert find_conda_solver(tmp_path) == tmp_path / "bin" / "mamba"

    def test_checks_condabin(self, tmp_path: Path) -> None:
        (tmp_path / "condabin").mkdir()
        (tmp_path / "condabin" / "micromamba").touch()
        assert find_conda_solver(tmp_path) == tmp_path / "condabin" / "micromamba"

    def test_returns_none_when_no_solver(self, tmp_path: Path) -> None:
        (tmp_path / "bin").mkdir()
        assert find_conda_solver(tmp_path) is None


class TestRuntimeSourceClassification:
    def test_system_source_for_none_path(self) -> None:
        assert ProjectService._runtime_source(None) == "system"

    def test_bundled_source_for_bundled_mamba_path(self, tmp_path: Path) -> None:
        bundled = tmp_path / ".local/share/blueprint-re/mamba/envs/blueprint-re-r"
        with patch("app.services.project_service.Path.home", return_value=tmp_path):
            assert ProjectService._runtime_source(bundled) == "bundled"

    def test_conda_source_for_user_conda_path(self, tmp_path: Path) -> None:
        conda_env = tmp_path / "miniconda3/envs/analysis"
        with patch("app.services.project_service.Path.home", return_value=tmp_path):
            assert ProjectService._runtime_source(conda_env) == "conda"

    def test_system_source_for_unrecognized_path(self, tmp_path: Path) -> None:
        with patch("app.services.project_service.Path.home", return_value=tmp_path):
            assert ProjectService._runtime_source("/usr/bin/Rscript") == "system"
