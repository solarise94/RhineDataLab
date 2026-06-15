"""Tests for resolver fallback behavior (W2).

Covers:
- W2-1: solver_error / timeout degrades to fallback_required under allow policy.
- W2-2: Bioconductor-only packages force the bioconductor family.
- W2-3: CRAN/Bioconductor mirror injection in R registry installs.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.manager_blueprint_tools import ManagerBlueprintTools
from app.services.runtime_dependency_resolver_service import (
    PACKAGE_STATUS_FALLBACK_REQUIRED,
    PACKAGE_STATUS_SOLVER_ERROR,
    RESOLVER_STATUS_FULLY_INSTALLABLE,
    RESOLVER_STATUS_SOLVER_ERROR,
    ProbeResult,
    RuntimeDependencyResolverService,
    RuntimeProbeResult,
    _select_r_fallback_family,
    collect_fallback_actions,
)


class ResolverFallbackDegradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = RuntimeDependencyResolverService(probe_timeout_seconds=1)

    def _patch_runtime_and_solver(self):
        return patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/micromamba"),
        )

    def test_solver_error_degrades_to_fallback_for_r(self) -> None:
        with self._patch_runtime_and_solver()[0], self._patch_runtime_and_solver()[1], patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="solver_error", error_code="timeout", error_detail="timed out"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {"ecosystem": "R", "runtime": "blueprint-re-r", "packages": ["ggplot2"]},
                policy="allow_safe_registry_install",
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_FULLY_INSTALLABLE)
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.installable), 1)
        self.assertEqual(plan.installable[0].installer, "cran")
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_FALLBACK_REQUIRED)

    def test_solver_error_degrades_to_fallback_for_python(self) -> None:
        with self._patch_runtime_and_solver()[0], self._patch_runtime_and_solver()[1], patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="solver_error", error_code="timeout", error_detail="timed out"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {"ecosystem": "python", "runtime": "python_env", "packages": ["numpy"]},
                policy="allow_safe_registry_install",
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_FULLY_INSTALLABLE)
        self.assertEqual(plan.installable[0].installer, "pip")
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_FALLBACK_REQUIRED)

    def test_solver_error_kept_under_report_only_policy(self) -> None:
        with self._patch_runtime_and_solver()[0], self._patch_runtime_and_solver()[1], patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="solver_error", error_code="timeout", error_detail="timed out"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {"ecosystem": "R", "runtime": "blueprint-re-r", "packages": ["ggplot2"]},
                policy="report_only",
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_SOLVER_ERROR)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_SOLVER_ERROR)

    def test_not_found_stays_fallback_required(self) -> None:
        with self._patch_runtime_and_solver()[0], self._patch_runtime_and_solver()[1], patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="not_found"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {"ecosystem": "R", "runtime": "blueprint-re-r", "packages": ["ggplot2"]},
                policy="allow_safe_registry_install",
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_FULLY_INSTALLABLE)
        self.assertEqual(plan.installable[0].installer, "cran")
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_FALLBACK_REQUIRED)


class BioconductorFamilySelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = RuntimeDependencyResolverService(probe_timeout_seconds=1)

    def _make_entry(self, name: str, fallback: list[str]):
        from app.services.runtime_dependency_resolver_service import (
            ResolverPackageEntry,
            _classify_for_ecosystem,
            _conda_candidates_for,
        )

        return ResolverPackageEntry(
            name=name,
            normalized_name=name.lower(),
            classification=_classify_for_ecosystem(name, "R"),
            conda_candidates=_conda_candidates_for(name, "R"),
            fallback_available=fallback,
            status=PACKAGE_STATUS_FALLBACK_REQUIRED,
        )

    def test_deseq2_forces_bioconductor(self) -> None:
        entries = [self._make_entry("DESeq2", ["cran", "bioconductor"])]
        self.assertEqual(_select_r_fallback_family(entries), "bioconductor")

    def test_mixed_deseq2_and_ggplot2_forces_bioconductor(self) -> None:
        entries = [
            self._make_entry("DESeq2", ["cran", "bioconductor"]),
            self._make_entry("ggplot2", ["cran", "bioconductor"]),
        ]
        self.assertEqual(_select_r_fallback_family(entries), "bioconductor")

    def test_pure_ggplot2_keeps_cran(self) -> None:
        entries = [self._make_entry("ggplot2", ["cran", "bioconductor"])]
        self.assertEqual(_select_r_fallback_family(entries), "cran")

    def test_deseq2_request_is_fully_installable_via_bioconductor(self) -> None:
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/micromamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="not_found"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {"ecosystem": "R", "runtime": "blueprint-re-r", "packages": ["DESeq2"]},
                policy="allow_safe_registry_install",
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_FULLY_INSTALLABLE)
        self.assertEqual(len(plan.installable), 1)
        self.assertEqual(plan.installable[0].installer, "bioconductor")
        self.assertEqual(plan.installable[0].name, "DESeq2")


class RRegistryMirrorTest(unittest.TestCase):
    def _make_tools(self, cran: str = "", bioc: str = "") -> ManagerBlueprintTools:
        settings = MagicMock()
        settings.cran_mirror = cran
        settings.bioconductor_mirror = bioc
        project_service = MagicMock()
        project_service.settings = settings
        tools = ManagerBlueprintTools.__new__(ManagerBlueprintTools)
        tools.project_service = project_service
        tools._run_dependency_command = MagicMock(return_value={"ok": True})
        tools._dependency_subprocess_env = MagicMock(return_value={})
        return tools

    def _patch_rscript(self):
        tmp = Path(tempfile.mkdtemp())
        rscript = tmp / "env" / "bin" / "Rscript"
        rscript.parent.mkdir(parents=True, exist_ok=True)
        rscript.touch()
        return patch(
            "app.services.manager_blueprint_tools.CommandTemplateWorkerAdapter._resolve_rscript_runtime",
            return_value=rscript,
        )

    def test_cran_install_uses_cran_mirror(self) -> None:
        tools = self._make_tools(cran="https://mirrors.tuna.tsinghua.edu.cn/CRAN")
        with self._patch_rscript():
            tools._run_r_registry_install(
                "proj",
                ecosystem="R",
                runtime="blueprint-re-r",
                names=["ggplot2"],
                installer_type="cran",
                timeout=600,
                started_at="2026-06-15T00:00:00Z",
            )
        call_args = tools._run_dependency_command.call_args
        # _run_dependency_command(project_id, command, ecosystem=..., ...)
        command = call_args.kwargs.get("command") if call_args.kwargs else None
        if command is None:
            command = call_args[0][1]
        expression = command[-1]
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/CRAN", expression)
        self.assertNotIn("cloud.r-project.org", expression)

    def test_bioconductor_install_uses_mirrors_and_correct_order(self) -> None:
        tools = self._make_tools(
            cran="https://mirrors.tuna.tsinghua.edu.cn/CRAN",
            bioc="https://mirrors.tuna.tsinghua.edu.cn/bioconductor",
        )
        with self._patch_rscript():
            tools._run_r_registry_install(
                "proj",
                ecosystem="R",
                runtime="blueprint-re-r",
                names=["DESeq2"],
                installer_type="bioconductor",
                timeout=600,
                started_at="2026-06-15T00:00:00Z",
            )
        call_args = tools._run_dependency_command.call_args
        # _run_dependency_command(project_id, command, ecosystem=..., ...)
        command = call_args.kwargs.get("command") if call_args.kwargs else None
        if command is None:
            command = call_args[0][1]
        expression = command[-1]
        # CRAN mirror is set.
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/CRAN", expression)
        # BiocManager is installed before BioC_mirror option is set.
        install_biocmanager_pos = expression.find('install.packages("BiocManager")')
        bioc_mirror_pos = expression.find("BioC_mirror")
        self.assertGreater(install_biocmanager_pos, 0)
        self.assertGreater(bioc_mirror_pos, install_biocmanager_pos)
        # Bare repositories() must not appear before BiocManager is loaded.
        self.assertNotRegex(expression, r'repositories\s*\(')
        # BiocManager::install is the final call.
        self.assertIn("BiocManager::install", expression)

    def test_empty_mirrors_fall_back_to_official(self) -> None:
        tools = self._make_tools()
        with self._patch_rscript():
            tools._run_r_registry_install(
                "proj",
                ecosystem="R",
                runtime="blueprint-re-r",
                names=["ggplot2"],
                installer_type="cran",
                timeout=600,
                started_at="2026-06-15T00:00:00Z",
            )
        call_args = tools._run_dependency_command.call_args
        # _run_dependency_command(project_id, command, ecosystem=..., ...)
        command = call_args.kwargs.get("command") if call_args.kwargs else None
        if command is None:
            command = call_args[0][1]
        expression = command[-1]
        self.assertIn("cloud.r-project.org", expression)
