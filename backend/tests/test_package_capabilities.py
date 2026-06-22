"""Tests for PackageService._resolve_capabilities registry-failure handling (#4 / audit §4.1).

A registry that FAILS TO LOAD must be reported as an explicit "registry load
failed" blocker, NOT silently collapsed into an empty set (which would emit a
false "Required skill/MCP not found" for every required capability and hide the
real cause). A registry that loads successfully but is genuinely empty must
still correctly yield "not found".
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.models.library import LibraryEntry, LibraryRegistry
from app.models.packages import PackageCompatibility, PackageManifest
from app.services.package_service import PackageService


def _registry(kind: str, *ids: str) -> LibraryRegistry:
    items = [
        LibraryEntry(id=item_id, kind=kind, name=item_id, summary_short="")
        for item_id in ids
    ]
    return LibraryRegistry(kind=kind, items=items)


def _manifest(**compat) -> PackageManifest:
    return PackageManifest(
        package_id="pkg-1",
        title="Pkg 1",
        compatibility=PackageCompatibility(**compat),
    )


class ResolveCapabilitiesTest(unittest.TestCase):
    def _service(self, ensure_side_effect) -> PackageService:
        registry_service = MagicMock()
        registry_service._ensure_registry.side_effect = ensure_side_effect
        return PackageService(registry_service, MagicMock(), settings=MagicMock())

    def test_load_failure_emits_load_blocker_not_false_not_found(self):
        # Skill registry raises (e.g. corrupt JSON / IO error); MCP loads empty.
        def ensure(kind):
            if kind == "skill":
                raise OSError("disk read error")
            return _registry("mcp")

        svc = self._service(ensure)
        warnings, blockers = svc._resolve_capabilities(
            _manifest(required_skills=["align", "qc"], required_mcps=["m1"])
        )

        # The skill failure is reported honestly, ONCE, with the cause.
        self.assertTrue(any("Registry load failed for skill" in b for b in blockers), blockers)
        self.assertTrue(any("disk read error" in b for b in blockers), blockers)
        # No false per-skill "not found" — the registry state is unknown, not empty.
        self.assertFalse(any("Required skill not found" in b for b in blockers), blockers)
        # The MCP registry loaded fine but is genuinely empty → real "not found" stands.
        self.assertIn("Required MCP not found: m1", blockers)

    def test_both_registries_failing_emit_two_load_blockers(self):
        svc = self._service(lambda kind: (_ for _ in ()).throw(RuntimeError(f"{kind} boom")))
        _warnings, blockers = svc._resolve_capabilities(
            _manifest(required_skills=["s1"], required_mcps=["m1"])
        )
        self.assertTrue(any("Registry load failed for skill" in b for b in blockers), blockers)
        self.assertTrue(any("Registry load failed for mcp" in b for b in blockers), blockers)
        self.assertFalse(any("not found" in b for b in blockers), blockers)

    def test_genuinely_empty_registry_emits_not_found(self):
        svc = self._service(lambda kind: _registry(kind))  # loads fine, no items
        _warnings, blockers = svc._resolve_capabilities(
            _manifest(required_skills=["s1"], required_mcps=["m1"])
        )
        self.assertIn("Required skill not found: s1", blockers)
        self.assertIn("Required MCP not found: m1", blockers)
        self.assertFalse(any("Registry load failed" in b for b in blockers), blockers)

    def test_present_capabilities_produce_no_blocker(self):
        def ensure(kind):
            return _registry("skill", "s1") if kind == "skill" else _registry("mcp", "m1")

        svc = self._service(ensure)
        warnings, blockers = svc._resolve_capabilities(
            _manifest(required_skills=["s1"], required_mcps=["m1"])
        )
        self.assertEqual(blockers, [])
        self.assertEqual(warnings, [])

    def test_optional_missing_is_warning_not_blocker(self):
        svc = self._service(lambda kind: _registry(kind))
        warnings, blockers = svc._resolve_capabilities(
            _manifest(optional_skills=["s_opt"], optional_mcps=["m_opt"])
        )
        self.assertEqual(blockers, [])
        self.assertIn("Optional skill not found: s_opt", warnings)
        self.assertIn("Optional MCP not found: m_opt", warnings)

    def test_load_failure_suppresses_optional_warnings_too(self):
        # When the registry can't be read, optional checks are skipped (state unknown),
        # so no false "optional not found" warnings either.
        svc = self._service(lambda kind: (_ for _ in ()).throw(OSError("io")))
        warnings, blockers = svc._resolve_capabilities(
            _manifest(optional_skills=["s_opt"], optional_mcps=["m_opt"])
        )
        self.assertFalse(any("Optional skill not found" in w for w in warnings), warnings)
        self.assertFalse(any("Optional MCP not found" in w for w in warnings), warnings)
        self.assertTrue(any("Registry load failed for skill" in b for b in blockers), blockers)
        self.assertTrue(any("Registry load failed for mcp" in b for b in blockers), blockers)


if __name__ == "__main__":
    unittest.main()
