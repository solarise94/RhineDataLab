"""Unit tests for the runtime dependency resolver service (P1)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.runtime_dependency_resolver_service import (
    PACKAGE_STATUS_CONDA_INSTALLABLE,
    PACKAGE_STATUS_FALLBACK_REQUIRED,
    PACKAGE_STATUS_MANUAL_PREPARATION_REQUIRED,
    PACKAGE_STATUS_RUNTIME_MISSING,
    PACKAGE_STATUS_SOLVER_ERROR,
    PACKAGE_STATUS_UNSUPPORTED_SOURCE_SPEC,
    RESOLVER_STATUS_FALLBACK_AVAILABLE_BUT_AMBIGUOUS,
    RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS,
    RESOLVER_STATUS_FULLY_INSTALLABLE,
    RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED,
    RESOLVER_STATUS_PARTIAL_RESOLUTION,
    RESOLVER_STATUS_RUNTIME_MISSING,
    RESOLVER_STATUS_SOLVER_ERROR,
    RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC,
    RESOLVER_TO_P0_FIELDS,
    ProbeResult,
    RuntimeDependencyResolverService,
    RuntimeProbeResult,
    collect_fallback_actions,
    fallback_policy_allows,
    is_registry_fallback_action_safe,
    normalize_fallback_policy,
)


class ResolverServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = RuntimeDependencyResolverService(probe_timeout_seconds=1)

    def test_invalid_ecosystem(self) -> None:
        plan = self.resolver.resolve("proj", {"ecosystem": "ruby", "runtime": "r_env", "packages": ["x"]})
        self.assertEqual(plan.status, "resolution_unknown")
        self.assertEqual(plan.error_code, "dependency_resolution_unknown")

    def test_system_runtime_short_circuits(self) -> None:
        plan = self.resolver.resolve(
            "proj", {"ecosystem": "python", "runtime": "__system__", "packages": ["numpy"]}
        )
        self.assertEqual(plan.status, RESOLVER_STATUS_RUNTIME_MISSING)
        self.assertEqual(plan.error_code, "dependency_install_start_failed")
        self.assertEqual(plan.retry_hint, "manual_runtime_preparation_required")

    def test_empty_packages(self) -> None:
        plan = self.resolver.resolve(
            "proj", {"ecosystem": "python", "runtime": "python_env", "packages": []}
        )
        self.assertEqual(plan.status, RESOLVER_STATUS_MANUAL_PREPARATION_REQUIRED)
        self.assertFalse(plan.ok)

    def test_source_spec_rejected(self) -> None:
        plan = self.resolver.resolve(
            "proj",
            {
                "ecosystem": "python",
                "runtime": "python_env",
                "packages": ["pydeseq2", "https://github.com/foo/bar"],
            },
        )
        self.assertEqual(plan.status, RESOLVER_STATUS_UNSUPPORTED_SOURCE_SPEC)
        self.assertEqual(plan.error_code, "github_source_install_not_supported")
        self.assertFalse(plan.ok)
        # Both packages are inspected; the source one is blocked, the bare one
        # is included as a separate entry.
        names = [pkg.name for pkg in plan.packages]
        self.assertIn("https://github.com/foo/bar", names)
        self.assertIn("pydeseq2", names)

    def test_partial_resolution_classification(self) -> None:
        # Force the runtime probe to succeed and conda probe to find only one
        # of the two packages.
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=None,
        ):
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy", "pydeseq2"],
                },
                policy="report_only",
            )
        # No conda solver means both packages need a fallback, status falls back
        # to "fallback_available_but_policy_disallows" because both are pip-fallback.
        self.assertEqual(plan.status, RESOLVER_STATUS_FALLBACK_AVAILABLE_POLICY_DISALLOWS)
        self.assertEqual(plan.error_code, "package_not_found_in_conda_channels")
        self.assertEqual(plan.retry_hint, "choose_fallback")

    def test_fully_installable_with_conda_solver(self) -> None:
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="found", match="numpy"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                },
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_FULLY_INSTALLABLE)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.installable[0].installer, "conda")
        self.assertEqual(plan.installable[0].candidate, "numpy")
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_CONDA_INSTALLABLE)

    def test_partial_with_conda_solver(self) -> None:
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            side_effect=[ProbeResult(status="found", match="r-ggplot2"), ProbeResult(status="not_found")],
        ):
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "R",
                    "runtime": "R_env",
                    "packages": ["ggplot2", "limma"],
                },
            )
        # ggplot2 is conda-installable, limma is not → mixed plan → partial.
        # P1.3: the plan shows what IS installable, but status blocks the job.
        self.assertEqual(plan.status, RESOLVER_STATUS_PARTIAL_RESOLUTION)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.error_code, RESOLVER_STATUS_PARTIAL_RESOLUTION)
        self.assertEqual(plan.retry_hint, "manual_preparation_required")
        self.assertEqual(len(plan.installable), 1)
        self.assertEqual(plan.installable[0].candidate, "r-ggplot2")
        self.assertEqual(len(plan.blocked), 1)
        self.assertEqual(plan.blocked[0].name, "limma")
        self.assertEqual(plan.blocked[0].reason, "package_not_found_in_conda_channels")
        self.assertIn("bioconductor", plan.blocked[0].fallback_available)

    def test_runtime_missing_blocks_all(self) -> None:
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=False, error_message="Python runtime not found"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "missing_env",
                    "packages": ["numpy", "pandas"],
                },
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_RUNTIME_MISSING)
        self.assertEqual(plan.error_code, "dependency_install_start_failed")
        for pkg in plan.packages:
            self.assertEqual(pkg.status, PACKAGE_STATUS_RUNTIME_MISSING)
        self.assertFalse(plan.installable)

    def test_request_dedupe_key_matches_state_service(self) -> None:
        plan = self.resolver.resolve(
            "proj",
            {
                "ecosystem": "R",
                "runtime": "R_env",
                "packages": ["limma", "ggplot2"],
            },
        )
        self.assertIn("dep:R:R_env", plan.request_dedupe_key)
        self.assertIn("ggplot2,limma", plan.request_dedupe_key)

    def test_resolver_to_p0_fields_mapping(self) -> None:
        # Every request-level status used by the resolver must map to a P0
        # normalized error_code / retry_hint pair.
        for status, mapping in RESOLVER_TO_P0_FIELDS.items():
            self.assertIn("error_code", mapping)
            self.assertIn("retry_hint", mapping)
        # And both fully_installable and runtime_missing should be present.
        self.assertIn(RESOLVER_STATUS_FULLY_INSTALLABLE, RESOLVER_TO_P0_FIELDS)
        self.assertIn(RESOLVER_STATUS_RUNTIME_MISSING, RESOLVER_TO_P0_FIELDS)

    def test_dedupe_repeated_packages(self) -> None:
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="found", match="numpy"),
        ):
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy", "NumPy", "numpy"],
                },
            )
        # Only one entry should be inspected.
        self.assertEqual(len(plan.packages), 1)
        self.assertEqual(plan.packages[0].name, "numpy")

    def test_cache_returns_same_match(self) -> None:
        calls: list[str] = []
        cache = self.resolver._cache

        def _probe(conda_bin, candidates, *, ecosystem, extra_channels=None):
            calls.append(",".join(candidates))
            return ProbeResult(status="found", match=candidates[0]) if candidates else ProbeResult(status="not_found")

        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            side_effect=_probe,
        ):
            self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                },
            )
            # Second call should be served from cache, not _probe_conda.
            self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                },
            )
        self.assertEqual(len(calls), 1)


    def test_invalid_grammar_specs_are_rejected_as_unsupported_source(self) -> None:
        """numpy>=1.0, package[extra], and similar non-bare specs must be unsupported_source_spec."""
        # Avoid subprocess calls from _channel_signature / _configured_channels.
        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return f"mock:{getattr(conda_bin, 'name', conda_bin)}:{ecosystem}:{runtime}"

        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_channel_signature",
            _mock_channel_sig,
        ):
            # Version comparison (>=) is rejected.
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy>=1.0"],
                },
            )
            self.assertEqual(plan.status, "unsupported_source_spec")
            self.assertEqual(plan.error_code, "github_source_install_not_supported")
            self.assertEqual(plan.packages[0].status, "unsupported_source_spec")

            # Extras syntax is rejected.
            plan2 = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["package[extra]"],
                },
            )
            self.assertEqual(plan2.status, "unsupported_source_spec")
            self.assertEqual(plan2.packages[0].status, "unsupported_source_spec")

            # Bare name is fine.
            plan3 = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                },
            )
            self.assertNotEqual(plan3.status, "unsupported_source_spec")

            # Exact version pin is fine.
            plan4 = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy==1.26.4"],
                },
            )
            self.assertNotEqual(plan4.status, "unsupported_source_spec")

    def test_plain_conda_passes_extra_channels_to_json_search(self) -> None:
        """When solver is conda (not mamba), _json_search_single receives extra_channels."""
        captured_calls: list[list[str]] = []

        def _mock_json_search(_self, conda_bin, candidate, extra_channels=None):
            captured_calls.append(extra_channels or [])
            return ProbeResult(status="found", match=candidate)

        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return "mock-sig"

        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/r_env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/conda"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_json_search_single",
            _mock_json_search,
        ), patch.object(
            RuntimeDependencyResolverService,
            "_channel_signature",
            _mock_channel_sig,
        ):
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "R",
                    "runtime": "r_env",
                    "packages": ["DESeq2"],
                    "channels": ["bioconda"],
                },
            )
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_CONDA_INSTALLABLE)
        self.assertTrue(len(captured_calls) >= 1)
        self.assertIn("conda-forge", captured_calls[0])
        self.assertIn("bioconda", captured_calls[0])

    def test_batch_prefetch_timeout_caches_solver_error(self) -> None:
        """Batch prefetch timeout must cache solver_error to prevent N serial fallback probes."""
        per_pkg_probe_calls: list[str] = []

        def _probe(_self, conda_bin, candidates, *, ecosystem, extra_channels=None):
            per_pkg_probe_calls.append(",".join(candidates))
            return ProbeResult(status="found", match=candidates[0]) if candidates else ProbeResult(status="not_found")

        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return "mock-sig"

        subprocess_calls: list[list[str]] = []
        def _timeout_subprocess(cmd, *args, **kwargs):
            subprocess_calls.append(cmd if isinstance(cmd, list) else [cmd])
            raise subprocess.TimeoutExpired(cmd="mamba repoquery", timeout=60)

        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_channel_signature",
            _mock_channel_sig,
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            _probe,
        ), patch(
            "app.services.runtime_dependency_resolver_service.subprocess.run",
            side_effect=_timeout_subprocess,
        ):
            plan = self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy", "pandas", "scipy"],
                },
                policy="report_only",
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_SOLVER_ERROR)
        for entry in plan.packages:
            self.assertEqual(entry.status, PACKAGE_STATUS_SOLVER_ERROR)
        self.assertEqual(per_pkg_probe_calls, [], "Per-package _probe_conda must NOT be called after batch timeout")
        self.assertEqual(len(subprocess_calls), 1, "Only one batch subprocess call should be attempted")

    def test_override_channels_flag_in_probe_commands(self) -> None:
        """All probe commands must use --override-channels when extra_channels are specified."""
        captured_cmds: list[list[str]] = []

        def _capture_subprocess(cmd, *args, **kwargs):
            captured_cmds.append(cmd if isinstance(cmd, list) else [cmd])
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout=json.dumps({"result": {"pkgs": [{"name": "r-tidyverse"}]}}),
                stderr="",
            )

        def _mock_channel_sig(_self, conda_bin, ecosystem, runtime, *, conda_base=None, extra_channels=None):
            return "mock-sig"

        # Test 1: mamba repoquery (batch prefetch + per-package _repoquery_search)
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/r_env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_channel_signature",
            _mock_channel_sig,
        ), patch(
            "app.services.runtime_dependency_resolver_service.subprocess.run",
            side_effect=_capture_subprocess,
        ):
            self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "R",
                    "runtime": "r_env",
                    "packages": ["tidyverse"],
                    "channels": ["bioconda"],
                },
            )
        for cmd in captured_cmds:
            if "repoquery" in cmd:
                self.assertIn("--override-channels", cmd,
                              f"repoquery command missing --override-channels: {cmd}")
                self.assertIn("-c", cmd)

        # Test 2: plain conda search (_json_search_single)
        captured_cmds.clear()
        self.resolver._cache.clear()
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/r_env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/conda"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_channel_signature",
            _mock_channel_sig,
        ), patch(
            "app.services.runtime_dependency_resolver_service.subprocess.run",
            side_effect=_capture_subprocess,
        ):
            self.resolver.resolve(
                "proj",
                {
                    "ecosystem": "R",
                    "runtime": "r_env",
                    "packages": ["tidyverse"],
                    "channels": ["bioconda"],
                },
            )
        for cmd in captured_cmds:
            if "search" in cmd:
                self.assertIn("--override-channels", cmd,
                              f"conda search command missing --override-channels: {cmd}")
                self.assertIn("-c", cmd)


class ResolverFallbackPolicyTest(unittest.TestCase):
    def test_normalize_policy(self) -> None:
        self.assertEqual(normalize_fallback_policy(None), "allow_safe_registry_install")
        self.assertEqual(normalize_fallback_policy(""), "allow_safe_registry_install")
        self.assertEqual(normalize_fallback_policy("report_only"), "report_only")
        self.assertEqual(
            normalize_fallback_policy("allow_safe_registry_install"),
            "allow_safe_registry_install",
        )
        self.assertEqual(normalize_fallback_policy("unknown"), "allow_safe_registry_install")

    def test_fallback_policy_allows(self) -> None:
        self.assertFalse(fallback_policy_allows(None))
        self.assertFalse(fallback_policy_allows("report_only"))
        self.assertTrue(fallback_policy_allows("allow_safe_registry_install"))

    def test_collect_fallback_actions_report_only(self) -> None:
        resolver = RuntimeDependencyResolverService()
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="not_found"),
        ):
            plan = resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["pydeseq2"],
                },
            )
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_FALLBACK_REQUIRED)
        self.assertEqual(collect_fallback_actions(plan, policy="report_only"), [])

    def test_collect_fallback_actions_allow_safe_registry(self) -> None:
        resolver = RuntimeDependencyResolverService()
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="not_found"),
        ):
            plan = resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["pydeseq2"],
                },
            )
        actions = collect_fallback_actions(plan, policy="allow_safe_registry_install")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].installer, "pip")
        self.assertEqual(actions[0].name, "pydeseq2")
        self.assertTrue(is_registry_fallback_action_safe(actions[0]))

    def test_r_fallback_prefers_cran_under_allow_policy(self) -> None:
        resolver = RuntimeDependencyResolverService()
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env/bin/Rscript")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/mamba"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(status="not_found"),
        ):
            plan = resolver.resolve(
                "proj",
                {
                    "ecosystem": "R",
                    "runtime": "R_env",
                    "packages": ["ggplot2"],
                },
                policy="allow_safe_registry_install",
            )

        self.assertEqual(plan.status, RESOLVER_STATUS_FULLY_INSTALLABLE)
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.installable), 1)
        self.assertEqual(plan.installable[0].installer, "cran")
        actions = collect_fallback_actions(plan, policy="allow_safe_registry_install")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].installer, "cran")

    def test_solver_error_classification(self) -> None:
        """ProbeResult distinguishes 'not_found' from 'solver_error'."""
        resolver = RuntimeDependencyResolverService()

        # PackagesNotFoundError → not_found
        not_found_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="PackagesNotFoundError: no match"
        )
        with patch("subprocess.run", return_value=not_found_result):
            result = resolver._json_search_single(Path("/usr/bin/conda"), "numpy")
        self.assertEqual(result.status, "not_found")

        # ProxyError → solver_error
        proxy_error_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="ProxyError: cannot connect"
        )
        with patch("subprocess.run", return_value=proxy_error_result):
            result = resolver._json_search_single(Path("/usr/bin/conda"), "numpy")
        self.assertEqual(result.status, "solver_error")
        self.assertEqual(result.error_code, "conda_solver_error")

        # NoWritablePkgsDirError → solver_error
        pkgs_dir_error = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="NoWritablePkgsDirError"
        )
        with patch("subprocess.run", return_value=pkgs_dir_error):
            result = resolver._json_search_single(Path("/usr/bin/conda"), "numpy")
        self.assertEqual(result.status, "solver_error")

        # Success with match → found
        success_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"numpy": [{"name": "numpy", "version": "1.26.0"}]}',
            stderr="",
        )
        with patch("subprocess.run", return_value=success_result):
            result = resolver._json_search_single(Path("/usr/bin/conda"), "numpy")
        self.assertEqual(result.status, "found")
        self.assertEqual(result.match, "numpy")

    def test_solver_error_prevents_fallback(self) -> None:
        """solver_error must not auto-fallback to CRAN/pip under report_only policy."""
        resolver = RuntimeDependencyResolverService()
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(present=True, resolved_path=Path("/tmp/env")),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_resolve_conda_solver",
            return_value=Path("/usr/bin/conda"),
        ), patch.object(
            RuntimeDependencyResolverService,
            "_probe_conda",
            return_value=ProbeResult(
                status="solver_error",
                error_code="conda_solver_error",
                error_detail="ProxyError",
            ),
        ):
            plan = resolver.resolve(
                "proj",
                {
                    "ecosystem": "python",
                    "runtime": "python_env",
                    "packages": ["numpy"],
                },
                policy="report_only",
            )
        self.assertEqual(plan.status, RESOLVER_STATUS_SOLVER_ERROR)
        self.assertEqual(plan.error_code, "dependency_probe_failed")
        self.assertEqual(plan.retry_hint, "inspect_stderr")
        self.assertEqual(plan.packages[0].status, PACKAGE_STATUS_SOLVER_ERROR)

    def test_runtime_affine_solver_selection(self) -> None:
        """Resolver picks the solver from the runtime's own conda base."""
        resolver = RuntimeDependencyResolverService()
        # Simulate a runtime in miniforge3 while configured base is miniconda3.
        with patch.object(
            RuntimeDependencyResolverService,
            "_probe_runtime",
            return_value=RuntimeProbeResult(
                present=True,
                resolved_path=Path("/home/user/miniforge3/envs/myenv"),
            ),
        ), patch(
            "app.services.runtime_dependency_resolver_service.find_conda_solver"
        ) as mock_find:
            # First call for runtime-derived base returns mamba.
            # Second call (fallback) would return conda, but we never reach it.
            def _find(base):
                if "miniforge3" in str(base):
                    return Path("/home/user/miniforge3/bin/mamba")
                return Path("/home/user/miniconda3/bin/conda")

            mock_find.side_effect = _find
            solver = resolver._resolve_conda_solver(
                "python", Path("/home/user/miniforge3/envs/myenv")
            )
        self.assertEqual(str(solver), "/home/user/miniforge3/bin/mamba")
        # find_conda_solver should have been called with miniforge3 first.
        calls = [str(c.args[0]) for c in mock_find.call_args_list]
        self.assertIn("/home/user/miniforge3", calls[0])

    def test_is_registry_fallback_action_safe_rejects_shell_chars(self) -> None:
        # A name with a shell metacharacter must not be allowed even under
        # the relaxed policy.
        self.assertFalse(is_registry_fallback_action_safe({
            "installer": "pip",
            "name": "numpy; rm -rf /",
        }))
        self.assertFalse(is_registry_fallback_action_safe({
            "installer": "pip",
            "name": "https://github.com/x/y",
        }))
        self.assertFalse(is_registry_fallback_action_safe({
            "installer": "conda",
            "name": "numpy",
        }))

    def test_repoquery_parses_real_json_schema(self) -> None:
        """Resolver parses real micromamba --json output correctly."""
        resolver = RuntimeDependencyResolverService()
        # Real micromamba repoquery search --json schema
        stdout = json.dumps({
            "query": {"query": "r-tidyverse", "type": "search"},
            "result": {
                "msg": "",
                "pkgs": [
                    {"name": "r-tidyverse", "version": "2.0.0", "build": "r41h785f33e_0"},
                    {"name": "r-tidyverse", "version": "1.3.2", "build": "r40hc72bb7e_0"},
                ],
            },
        })
        result = resolver._parse_repoquery_output(stdout, ["r-tidyverse"])
        self.assertEqual(result.status, "found")
        self.assertEqual(result.match, "r-tidyverse")

    def test_repoquery_parses_tabular_output(self) -> None:
        """Resolver falls back to regex for tabular (non-JSON) mamba output."""
        resolver = RuntimeDependencyResolverService()
        # Simulate real mamba tabular output (no --json flag honored)
        stdout = (
            " Name        Version Build                       Channel        Subdir\n"
            "──────────────────────────────────────────────────────────────────────\n"
            " r-tidyverse 2.0.0   r41h785f33e_0 (+  8 builds) conda-forge    noarch\n"
            " r-tidyverse 1.3.2   r40hc72bb7e_0 (+  3 builds) conda-forge    noarch\n"
        )
        result = resolver._parse_repoquery_output(stdout, ["r-tidyverse"])
        self.assertEqual(result.status, "found")
        self.assertEqual(result.match, "r-tidyverse")

    def test_repoquery_not_found_json(self) -> None:
        """Empty pkgs list in JSON means package is absent."""
        resolver = RuntimeDependencyResolverService()
        stdout = json.dumps({
            "query": {"query": "nonexistent-pkg", "type": "search"},
            "result": {"msg": "No entries matching \"nonexistent-pkg\" found", "pkgs": [], "status": "OK"},
        })
        result = resolver._parse_repoquery_output(stdout, ["nonexistent-pkg"])
        self.assertEqual(result.status, "not_found")

    def test_repoquery_unparseable_nonblank_returns_solver_error(self) -> None:
        """Non-blank stdout that can't be parsed as JSON or tabular → solver_error."""
        resolver = RuntimeDependencyResolverService()
        stdout = "ERROR: something went wrong internally\nStack trace..."
        result = resolver._parse_repoquery_output(stdout, ["scanpy"])
        self.assertEqual(result.status, "solver_error")
        self.assertEqual(result.error_code, "unknown_probe_output")

    def test_repoquery_multi_candidate_json(self) -> None:
        """Multi-candidate search finds match among multiple packages in pkgs."""
        resolver = RuntimeDependencyResolverService()
        stdout = json.dumps({
            "query": {"query": "scanpy numpy", "type": "search"},
            "result": {
                "pkgs": [
                    {"name": "numpy", "version": "1.26.0"},
                    {"name": "numpy", "version": "1.25.0"},
                    {"name": "scanpy", "version": "1.10.0"},
                ],
            },
        })
        # First candidate in the list wins
        result = resolver._parse_repoquery_output(stdout, ["scanpy", "numpy"])
        self.assertEqual(result.status, "found")
        self.assertEqual(result.match, "scanpy")

    def test_repoquery_json_with_progress_prefix(self) -> None:
        """stdout with progress text before JSON is still parsed correctly."""
        resolver = RuntimeDependencyResolverService()
        progress = "Getting repodata from channels...\nconda-forge/linux-64  Using cache\n"
        json_body = json.dumps({
            "query": {"query": "scanpy", "type": "search"},
            "result": {"pkgs": [{"name": "scanpy", "version": "1.10.0"}]},
        })
        result = resolver._parse_repoquery_output(progress + json_body, ["scanpy"])
        self.assertEqual(result.status, "found")
        self.assertEqual(result.match, "scanpy")

    def test_configured_channels_micromamba_syntax(self) -> None:
        """_configured_channels uses 'config list --json' for micromamba."""
        from app.services.runtime_dependency_resolver_service import _configured_channels

        fake_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"channels": ["conda-forge", "defaults"]}),
            stderr="",
        )

        def _check_command(command, **kwargs):
            self.assertIn("config", command)
            self.assertIn("list", command)
            self.assertIn("--json", command)
            self.assertNotIn("--show", command)
            return fake_result

        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=_check_command):
            channels = _configured_channels(Path("/usr/bin/micromamba"), None)
        self.assertEqual(channels, ["conda-forge", "defaults"])

    def test_configured_channels_conda_syntax(self) -> None:
        """_configured_channels uses '--show channels --json' for conda/mamba."""
        from app.services.runtime_dependency_resolver_service import _configured_channels

        fake_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"channels": ["defaults", "bioconda"]}),
            stderr="",
        )

        def _check_command(command, **kwargs):
            self.assertIn("config", command)
            self.assertIn("--show", command)
            self.assertIn("--json", command)
            self.assertNotIn("list", command)
            return fake_result

        with patch("app.services.runtime_dependency_resolver_service.subprocess.run", side_effect=_check_command):
            channels = _configured_channels(Path("/usr/bin/mamba"), None)
        self.assertEqual(channels, ["defaults", "bioconda"])


if __name__ == "__main__":
    unittest.main()
