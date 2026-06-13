"""Tests for the Card Library (牌库) system.

Covers:
- Blueprint CRUD (save from card, import, update, delete, get, list, search).
- Desensitization (path stripping, asset_id stripping, secret blocking).
- Cover image validation (magic bytes, size, SVG rejection).
- Instantiation validation:
  - Required inputs must be bound.
  - Required parameters must be provided.
  - Skill/MCP availability check.
  - Runtime requirements enforcement.
  - Project existence check.
  - Project lock usage.
- BlueprintOutputSchema.artifact_class uses ArtifactClass literal.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.models.card_blueprint import (
    BlueprintOutputSchema,
    BlueprintRuntimeRequirement,
    BlueprintRuntimeRequirements,
    CardBlueprint,
    CardBlueprintDraft,
    CardBlueprintIndexEntry,
    InstantiateRequest,
    ReferenceAssetRef,
)
from app.models.cards import Card, CardAssetRef
from app.models.executor import ExecutorContext, RuntimeBindings
from app.models.graph import Asset
from app.models.output_contracts import CardOutputSpec
from app.services.card_desensitization_service import GeneralizedBlueprint
from app.services.card_library_service import CardLibraryService
from app.services.project_service import ProjectService
from app.services.reference_data_service import ReferenceDataService
from app.services.utils import atomic_write_json


class _Base(unittest.TestCase):
    def setUp(self):
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root)
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()
        shutil.rmtree(self.data_root, ignore_errors=True)

    def _project_service(self):
        with patch("app.services.project_service.get_settings", return_value=self.settings):
            return ProjectService()

    def _service(self, project_service=None):
        ps = project_service or self._project_service()
        return CardLibraryService(ps, settings=self.settings)

    def _add_asset(
        self,
        project_id: str,
        asset_id: str,
        path: str = "results/data.csv",
        status: str = "valid",
    ) -> None:
        ps = self._project_service()
        store = ps.graph_store(project_id)
        assets = store.load_assets()
        assets.append(
            Asset(
                asset_id=asset_id,
                asset_type="data",
                title="Test Asset",
                status=status,
                path=path,
                summary="test asset",
            )
        )
        store.save_assets(assets)

    def _create_project(self, project_id: str = "test-project"):
        ps = self._project_service()
        ps.create_project(
            project_id=project_id,
            name="Test Project",
            current_goal="testing",
        )
        return ps

    def _create_card_with_runtime(self, project_id: str, card_id: str, title: str):
        ps = self._project_service()
        store = ps.graph_store(project_id)
        card = Card(
            card_id=card_id,
            card_type="module",
            title=title,
            status="proposed",
            summary="A reusable analysis card",
            outputs=[
                CardOutputSpec(role="result", label="Result", artifact_class="figure"),
            ],
            executor_context=ExecutorContext(
                instruction_blocks=["Run analysis"],
                runtime_bindings=RuntimeBindings(
                    conda_env="scanpy",
                    r_env="__system__",
                ),
            ),
        )
        store.save_cards([card])
        return card


# ======================================================================
# Blueprint CRUD
# ======================================================================

class TestBlueprintCRUD(_Base):
    def test_save_from_card(self):
        ps = self._create_project("proj1")
        svc = self._service(ps)

        # Create a card in the project
        store = ps.graph_store("proj1")
        card = Card(
            card_id="card-001",
            card_type="module",
            title="Test Card",
            status="proposed",
            summary="/home/user/data analysis",
            inputs=[CardAssetRef(label="input data", asset_id="sha256:" + "a" * 64)],
            outputs=[
                CardOutputSpec(role="result", label="Result", artifact_class="figure"),
            ],
        )
        store.save_cards([card])

        result = svc.save_from_card("proj1", "card-001")
        self.assertTrue(result.blueprint_id)
        self.assertIn("未进行 AI 泛化检查", result.warnings[0])

        # Verify blueprint saved
        bp = svc.get_blueprint(result.blueprint_id)
        self.assertEqual(bp["title"], "Test Card")
        # Desensitization: /home path stripped
        self.assertNotIn("/home/user", bp["summary"])

    def test_save_from_card_infers_formats(self):
        ps = self._create_project("proj1")
        svc = self._service(ps)
        store = ps.graph_store("proj1")
        asset_id = "sha256:" + "a" * 64
        store.save_assets([
            Asset(
                asset_id=asset_id,
                asset_type="data",
                title="Input",
                status="valid",
                path="results/counts.h5ad",
                summary="input",
            ),
        ])
        card = Card(
            card_id="card-002",
            card_type="module",
            title="Format Inference",
            status="proposed",
            summary="test",
            inputs=[CardAssetRef(label="input data", asset_id=asset_id)],
            outputs=[CardOutputSpec(role="result", label="Result", artifact_class="figure")],
        )
        store.save_cards([card])

        result = svc.save_from_card("proj1", "card-002")
        bp = svc.get_blueprint(result.blueprint_id)
        self.assertEqual(bp["inputs_schema"][0]["accepted_formats"], ["h5ad"])

    def test_import_blueprint(self):
        svc = self._service()
        bp_data = {
            "blueprint_id": "will-be-regenerated",
            "title": "Imported BP",
            "summary": "A test blueprint",
            "skills": ["skill_a"],
            "mcp_servers": ["mcp_b"],
            "inputs_schema": [{"slot": "data", "label": "Input Data", "required": True}],
            "outputs_schema": [
                {"role": "plot", "label": "Plot", "artifact_class": "figure"},
            ],
            "parameters": [
                {"name": "threshold", "type": "float", "required": True},
            ],
            "runtime_requirements": {
                "python": {"env_hint": "scanpy", "packages": ["scanpy"]},
                "r": "__system__",
            },
        }
        result = svc.save_from_import(bp_data)
        self.assertTrue(result.blueprint_id)

        bp = svc.get_blueprint(result.blueprint_id)
        self.assertEqual(bp["title"], "Imported BP")
        self.assertEqual(bp["parameters"][0]["name"], "threshold")

    def test_list_blueprints(self):
        svc = self._service()
        svc.save_from_import({"blueprint_id": "x", "title": "BP 1"})
        svc.save_from_import({"blueprint_id": "y", "title": "BP 2"})
        entries = svc.list_blueprints()
        self.assertEqual(len(entries), 2)

    def test_search_blueprints(self):
        svc = self._service()
        svc.save_from_import({"blueprint_id": "x", "title": "RNA-seq Analysis", "domain": "genomics"})
        svc.save_from_import({"blueprint_id": "y", "title": "Cell Culture", "domain": "biology"})
        results = svc.search_blueprints(query="rna")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "RNA-seq Analysis")

    def test_update_blueprint(self):
        svc = self._service()
        result = svc.save_from_import({"blueprint_id": "x", "title": "Old Title"})
        from app.models.card_blueprint import UpdateBlueprintRequest
        updated = svc.update_blueprint(result.blueprint_id, UpdateBlueprintRequest(title="New Title"))
        self.assertEqual(updated["title"], "New Title")

    def test_delete_blueprint(self):
        svc = self._service()
        result = svc.save_from_import({"blueprint_id": "x", "title": "BP"})
        svc.delete_blueprint(result.blueprint_id)
        with self.assertRaises(ValueError):
            svc.get_blueprint(result.blueprint_id)

    def test_export_blueprint(self):
        svc = self._service()
        result = svc.save_from_import({"blueprint_id": "x", "title": "BP", "summary": "test"})
        exported = svc.export_blueprint(result.blueprint_id)
        self.assertEqual(exported["title"], "BP")

    def test_get_blueprint_fills_defaults_for_old_json(self):
        """Old v1 blueprints missing reference_assets/use_cases get default empty lists."""
        svc = self._service()
        result = svc.save_from_import({"blueprint_id": "x", "title": "BP"})
        bp_path = svc._blueprint_dir(result.blueprint_id) / "blueprint.json"
        old_data = {
            "blueprint_id": result.blueprint_id,
            "title": "BP",
            "summary": "",
            # Deliberately omit reference_assets and use_cases
        }
        atomic_write_json(bp_path, old_data)

        bp = svc.get_blueprint(result.blueprint_id)
        self.assertIn("reference_assets", bp)
        self.assertIn("use_cases", bp)
        self.assertEqual(bp["reference_assets"], [])
        self.assertEqual(bp["use_cases"], [])


# ======================================================================
# Desensitization
# ======================================================================

class TestDesensitization(_Base):
    def test_home_path_stripped(self):
        """Desensitization happens in save_from_card, not save_from_import."""
        ps = self._create_project("proj1")
        svc = self._service(ps)
        store = ps.graph_store("proj1")
        card = Card(
            card_id="c1", card_type="module", title="Card", status="proposed",
            summary="path /home/alice/data removed",
        )
        store.save_cards([card])
        result = svc.save_from_card("proj1", "c1")
        bp = svc.get_blueprint(result.blueprint_id)
        self.assertNotIn("/home/alice", bp["summary"])

    def test_users_path_stripped(self):
        ps = self._create_project("proj1")
        svc = self._service(ps)
        store = ps.graph_store("proj1")
        card = Card(
            card_id="c1", card_type="module", title="Card", status="proposed",
            summary="see /Users/bob/Desktop for details",
        )
        store.save_cards([card])
        result = svc.save_from_card("proj1", "c1")
        bp = svc.get_blueprint(result.blueprint_id)
        self.assertNotIn("/Users/bob", bp["summary"])

    def test_windows_path_stripped(self):
        ps = self._create_project("proj1")
        svc = self._service(ps)
        store = ps.graph_store("proj1")
        card = Card(
            card_id="c1", card_type="module", title="Card", status="proposed",
            summary="C:\\Users\\carol\\data here",
        )
        store.save_cards([card])
        result = svc.save_from_card("proj1", "c1")
        bp = svc.get_blueprint(result.blueprint_id)
        self.assertNotIn("C:\\Users\\carol", bp["summary"])

    def test_asset_id_stripped_from_card(self):
        ps = self._create_project("proj1")
        svc = self._service(ps)
        store = ps.graph_store("proj1")
        card = Card(
            card_id="c1",
            card_type="module",
            title="Card",
            status="proposed",
            summary="data sha256:" + "f" * 64 + " processed",
        )
        store.save_cards([card])
        result = svc.save_from_card("proj1", "c1")
        bp = svc.get_blueprint(result.blueprint_id)
        self.assertNotIn("sha256:", bp["summary"])


# ======================================================================
# Cover image validation
# ======================================================================

class TestCoverImage(_Base):
    def _save_blueprint(self, svc):
        result = svc.save_from_import({"blueprint_id": "x", "title": "BP"})
        return result.blueprint_id

    def test_save_png_cover(self):
        svc = self._service()
        bp_id = self._save_blueprint(svc)
        # Minimal valid PNG: 8-byte signature + IHDR chunk
        png_sig = b"\x89PNG\r\n\x1a\n"
        # Minimal IHDR chunk: length(4) + type(4) + data(13) + crc(4)
        ihdr = b"\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00" * 4
        content = png_sig + ihdr
        result = svc.save_cover(bp_id, content, "cover.png")
        self.assertTrue(result["ok"])

    def test_reject_svg_cover(self):
        svc = self._service()
        bp_id = self._save_blueprint(svc)
        with self.assertRaises(ValueError) as ctx:
            svc.save_cover(bp_id, b"<svg></svg>", "cover.svg")
        self.assertIn("SVG", str(ctx.exception))

    def test_reject_too_large(self):
        svc = self._service()
        bp_id = self._save_blueprint(svc)
        big = b"\x89PNG" + b"\x00" * (2 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError) as ctx:
            svc.save_cover(bp_id, big, "cover.png")
        self.assertIn("too large", str(ctx.exception).lower())

    def test_reject_invalid_magic_bytes(self):
        svc = self._service()
        bp_id = self._save_blueprint(svc)
        with self.assertRaises(ValueError) as ctx:
            svc.save_cover(bp_id, b"not-an-image", "cover.png")
        self.assertIn("Invalid image format", str(ctx.exception))


# ======================================================================
# Instantiation validation (Findings #1, #3, #4)
# ======================================================================

class TestInstantiationValidation(_Base):
    def _setup_project_with_blueprint(self, bp_overrides=None):
        """Create a project and import a blueprint. Returns (service, project_service, bp_id)."""
        ps = self._create_project("proj1")
        svc = self._service(ps)
        bp_data = {
            "blueprint_id": "test-bp",
            "title": "Test BP",
            "inputs_schema": [
                {"slot": "data", "label": "Input Data", "required": True},
            ],
            "outputs_schema": [
                {"role": "result", "label": "Result", "artifact_class": "table"},
            ],
            "parameters": [
                {"name": "threshold", "type": "float", "required": True},
            ],
            "skills": [],
            "mcp_servers": [],
            "runtime_requirements": {
                "python": {"env_hint": "", "packages": []},
                "r": "__system__",
            },
        }
        if bp_overrides:
            bp_data.update(bp_overrides)
        result = svc.save_from_import(bp_data)
        return svc, ps, result.blueprint_id

    def test_missing_required_parameter(self):
        svc, _, bp_id = self._setup_project_with_blueprint()
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "asset-1"},
            parameter_values={},  # Missing 'threshold'
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("threshold" in b for b in result.blockers))

    def test_missing_required_input(self):
        svc, _, bp_id = self._setup_project_with_blueprint()
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={},  # 'data' not bound
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("data" in b for b in result.blockers))

    def test_project_not_found(self):
        svc, _, bp_id = self._setup_project_with_blueprint()
        result = svc.instantiate(bp_id, "nonexistent-project", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("not found" in b.lower() for b in result.blockers))

    def test_runtime_requirement_enforced(self):
        bp_overrides = {
            "runtime_requirements": {
                "python": {"env_hint": "scanpy", "packages": ["scanpy"]},
                "r": "__system__",
            },
        }
        svc, _, bp_id = self._setup_project_with_blueprint(bp_overrides)
        self._add_asset("proj1", "a1")
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
            python_runtime=None,  # No runtime selected
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("python runtime" in b.lower() for b in result.blockers))

    def test_successful_instantiation(self):
        svc, ps, bp_id = self._setup_project_with_blueprint()
        self._add_asset("proj1", "asset-001", path="results/data.csv", status="valid")
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "asset-001"},
            parameter_values={"threshold": "0.05"},
        ))
        self.assertNotEqual(result.card_id, "")
        self.assertEqual(result.blockers, [])

        # Verify card was added to project
        cards = ps.graph_store("proj1").load_cards()
        self.assertTrue(any(c.card_id == result.card_id for c in cards))

    def test_parameter_with_path_blocked(self):
        svc, _, bp_id = self._setup_project_with_blueprint()
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "/home/user/secret"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("file path" in b for b in result.blockers))

    def test_parameter_with_secret_blocked(self):
        svc, _, bp_id = self._setup_project_with_blueprint()
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "my_api_key=ABC123"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("sensitive" in b.lower() for b in result.blockers))

    def test_skill_not_available_blocked(self):
        """Skill referenced by blueprint must exist in the library registry."""
        svc, ps, bp_id = self._setup_project_with_blueprint({
            "skills": ["nonexistent_skill"],
        })
        self._add_asset("proj1", "a1")
        # Mock the library registry service to report skill not found
        mock_registry = MagicMock()
        mock_registry.get_entry.side_effect = ValueError("skill library item not found: nonexistent_skill")
        svc.library_registry_service = mock_registry

        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("nonexistent_skill" in b for b in result.blockers))

    def test_mcp_not_available_blocked(self):
        """MCP server referenced by blueprint must exist in the library registry."""
        svc, ps, bp_id = self._setup_project_with_blueprint({
            "mcp_servers": ["nonexistent_mcp"],
        })
        self._add_asset("proj1", "a1")
        mock_registry = MagicMock()
        mock_registry.get_entry.side_effect = ValueError("mcp library item not found: nonexistent_mcp")
        svc.library_registry_service = mock_registry

        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("nonexistent_mcp" in b for b in result.blockers))

    def test_unknown_input_asset_blocked(self):
        """Binding a non-existent input asset must block instantiation."""
        svc, _, bp_id = self._setup_project_with_blueprint()
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "does-not-exist"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("does-not-exist" in b for b in result.blockers))

    def test_input_asset_unusable_status_blocked(self):
        """Binding a rejected/missing/archived asset must block instantiation."""
        svc, _, bp_id = self._setup_project_with_blueprint()
        self._add_asset("proj1", "rejected-asset", status="rejected")
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "rejected-asset"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("rejected-asset" in b for b in result.blockers))

    def test_input_asset_format_mismatch_blocked(self):
        """Binding an asset whose format is not in accepted_formats must block."""
        svc, _, bp_id = self._setup_project_with_blueprint({
            "inputs_schema": [
                {"slot": "data", "label": "Input Data", "required": True, "accepted_formats": ["tsv"]},
            ],
        })
        self._add_asset("proj1", "csv-asset", path="results/data.csv")
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "csv-asset"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("csv" in b.lower() and "tsv" in b.lower() for b in result.blockers))

    def test_extensionless_asset_blocked_when_formats_required(self):
        """An asset with no extension must be blocked if accepted_formats is specified."""
        svc, _, bp_id = self._setup_project_with_blueprint({
            "inputs_schema": [
                {"slot": "data", "label": "Input Data", "required": True, "accepted_formats": ["csv"]},
            ],
        })
        self._add_asset("proj1", "extless-asset", path="results/data")
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "extless-asset"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("no inferable format" in b.lower() for b in result.blockers))

    def test_disabled_skill_blocked(self):
        """A skill that exists but is disabled must block instantiation."""
        svc, _, bp_id = self._setup_project_with_blueprint({
            "skills": ["disabled_skill"],
        })
        self._add_asset("proj1", "a1")
        mock_registry = MagicMock()
        mock_registry.get_entry.return_value = {"item": {"enabled": False}}
        svc.library_registry_service = mock_registry

        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("disabled" in b.lower() for b in result.blockers))

    def test_disabled_mcp_blocked(self):
        """An MCP server that exists but is disabled must block instantiation."""
        svc, _, bp_id = self._setup_project_with_blueprint({
            "mcp_servers": ["disabled_mcp"],
        })
        self._add_asset("proj1", "a1")
        mock_registry = MagicMock()
        mock_registry.get_entry.return_value = {"item": {"enabled": False}}
        svc.library_registry_service = mock_registry

        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("disabled" in b.lower() for b in result.blockers))

    def test_env_hint_does_not_block_without_packages(self):
        """env_hint alone is a soft hint and must not require a runtime."""
        svc, ps, bp_id = self._setup_project_with_blueprint({
            "runtime_requirements": {
                "python": {"env_hint": "scanpy", "packages": []},
                "r": "__system__",
            },
        })
        self._add_asset("proj1", "a1")
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
            python_runtime=None,
        ))
        self.assertNotEqual(result.card_id, "")
        self.assertEqual(result.blockers, [])

    def test_invalid_runtime_with_packages_blocked(self):
        """A selected runtime that cannot be resolved must block instantiation."""
        svc, _, bp_id = self._setup_project_with_blueprint({
            "runtime_requirements": {
                "python": {"env_hint": "", "packages": ["scanpy"]},
                "r": "__system__",
            },
        })
        self._add_asset("proj1", "a1")
        result = svc.instantiate(bp_id, "proj1", InstantiateRequest(
            input_bindings={"data": "a1"},
            parameter_values={"threshold": "0.5"},
            python_runtime="totally-fake-env",
        ))
        self.assertEqual(result.card_id, "")
        self.assertTrue(any("runtime" in b.lower() for b in result.blockers))


# ======================================================================
# BlueprintOutputSchema.artifact_class validation (Finding #5)
# ======================================================================

class TestArtifactClassValidation(unittest.TestCase):
    def test_legal_artifact_classes(self):
        for cls in ("figure", "table", "document", "model", "archive", "binary"):
            bp = BlueprintOutputSchema(role="r", label="l", artifact_class=cls)
            self.assertEqual(bp.artifact_class, cls)

    def test_illegal_artifact_class_rejected(self):
        with self.assertRaises(ValidationError):
            BlueprintOutputSchema(role="r", label="l", artifact_class="bogus")

    def test_format_normalization(self):
        bp = BlueprintOutputSchema(
            role="r", label="l",
            artifact_class="figure",
            accepted_formats=["SVG", ".png", "PDF"],
            preferred_format="SVG",
        )
        self.assertEqual(bp.accepted_formats, ["svg", "png", "pdf"])
        self.assertEqual(bp.preferred_format, "svg")

    def test_preferred_format_must_match_accepted(self):
        with self.assertRaises(ValidationError):
            BlueprintOutputSchema(
                role="r", label="l",
                artifact_class="figure",
                accepted_formats=["png"],
                preferred_format="svg",
            )


# ======================================================================
# Project draft flow
# ======================================================================

class TestProjectDraftFlow(_Base):
    def test_create_project_draft(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        result = svc.create_project_draft("proj-draft", "card-001")
        self.assertTrue(result.draft_id)
        self.assertIn("请执行规则审查后再发布", result.warnings[0])

        draft_path = ps.project_path("proj-draft") / "card-library-drafts" / "drafts" / result.draft_id
        self.assertTrue(draft_path.exists())
        self.assertTrue((draft_path / "blueprint.json").exists())

    def test_get_project_draft(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        draft = svc.get_project_draft("proj-draft", created.draft_id)
        self.assertEqual(draft["draft_id"], created.draft_id)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["blueprint"]["title"], "Clean Card")

    def test_get_project_draft_fills_defaults_for_old_json(self):
        """Old drafts missing reference_assets/use_cases get default empty lists."""
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        draft_dir = ps.project_path("proj-draft") / "card-library-drafts" / "drafts" / created.draft_id
        old_data = {
            "draft_id": created.draft_id,
            "project_id": "proj-draft",
            "status": "draft",
            "blueprint": {
                "blueprint_id": "bp-old",
                "title": "Clean Card",
                "summary": "",
                # Deliberately omit reference_assets and use_cases
            },
        }
        atomic_write_json(draft_dir / "blueprint.json", old_data)

        draft = svc.get_project_draft("proj-draft", created.draft_id)
        self.assertIn("reference_assets", draft["blueprint"])
        self.assertIn("use_cases", draft["blueprint"])
        self.assertEqual(draft["blueprint"]["reference_assets"], [])
        self.assertEqual(draft["blueprint"]["use_cases"], [])

    def test_list_project_drafts(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        entries = svc.list_project_drafts("proj-draft")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["draft_id"], created.draft_id)
        self.assertEqual(entries[0]["status"], "draft")
        self.assertEqual(entries[0]["title"], "Clean Card")

    def test_review_project_draft_clean(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        result = svc.review_project_draft("proj-draft", created.draft_id)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["review"]["verdict"], "pass")

    def test_review_project_draft_project_name_in_title(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Test Project Analysis")

        created = svc.create_project_draft("proj-draft", "card-001")
        result = svc.review_project_draft("proj-draft", created.draft_id)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["review"]["verdict"], "fail")

    def test_review_project_draft_absolute_path_in_instructions(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        # Inject an absolute path after extraction scrubbed it
        draft = svc.get_project_draft("proj-draft", created.draft_id)
        draft["blueprint"]["instruction_blocks"] = ["Load data from /home/user/data.csv"]
        from app.services.utils import atomic_write_json
        draft_dir = ps.project_path("proj-draft") / "card-library-drafts" / "drafts" / created.draft_id
        atomic_write_json(draft_dir / "blueprint.json", draft)

        result = svc.review_project_draft("proj-draft", created.draft_id)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["review"]["verdict"], "fail")

    def test_publish_project_draft_approved(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        svc.review_project_draft("proj-draft", created.draft_id)
        result = svc.publish_project_draft("proj-draft", created.draft_id)

        self.assertTrue(result.global_blueprint_id)
        draft = svc.get_project_draft("proj-draft", created.draft_id)
        self.assertEqual(draft["status"], "published")
        self.assertEqual(draft["global_blueprint_id"], result.global_blueprint_id)

        # Verify global blueprint exists
        bp = svc.get_blueprint(result.global_blueprint_id)
        self.assertEqual(bp["title"], "Clean Card")

    def test_publish_project_draft_not_approved(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        with self.assertRaises(ValueError) as ctx:
            svc.publish_project_draft("proj-draft", created.draft_id)
        self.assertIn("approved", str(ctx.exception).lower())

    def test_delete_project_draft(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        result = svc.delete_project_draft("proj-draft", created.draft_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["draft_id"], created.draft_id)

        with self.assertRaises(ValueError):
            svc.get_project_draft("proj-draft", created.draft_id)

    def test_publish_project_draft_is_idempotent(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        svc.review_project_draft("proj-draft", created.draft_id)
        first = svc.publish_project_draft("proj-draft", created.draft_id)
        second = svc.publish_project_draft("proj-draft", created.draft_id)

        self.assertEqual(first.global_blueprint_id, second.global_blueprint_id)
        # Only one global blueprint should exist for this title.
        entries = svc.list_blueprints()
        self.assertEqual(len([e for e in entries if e.get("title") == "Clean Card"]), 1)

    def test_update_project_draft_resets_status_and_review(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Test Project Analysis")

        created = svc.create_project_draft("proj-draft", "card-001")
        svc.review_project_draft("proj-draft", created.draft_id)
        draft = svc.get_project_draft("proj-draft", created.draft_id)
        self.assertEqual(draft["status"], "rejected")
        self.assertIsNotNone(draft["review"])

        from app.models.card_blueprint import UpdateProjectDraftRequest
        result = svc.update_project_draft(
            "proj-draft",
            created.draft_id,
            UpdateProjectDraftRequest(
                title="Generic Analysis",
                summary="A reusable analysis template",
                tags=["analysis"],
                domain="bioinformatics",
                instruction_blocks=["Run standard analysis"],
                python_packages=["scanpy"],
            ),
        )
        updated = result["draft"]
        self.assertEqual(updated["status"], "draft")
        self.assertIsNone(updated["review"])
        self.assertEqual(updated["blueprint"]["title"], "Generic Analysis")
        self.assertEqual(updated["blueprint"]["tags"], ["analysis"])
        self.assertEqual(updated["blueprint"]["runtime_requirements"]["python"]["packages"], ["scanpy"])

    def test_update_project_draft_published_fails(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        svc.review_project_draft("proj-draft", created.draft_id)
        svc.publish_project_draft("proj-draft", created.draft_id)

        from app.models.card_blueprint import UpdateProjectDraftRequest
        with self.assertRaises(ValueError) as ctx:
            svc.update_project_draft(
                "proj-draft",
                created.draft_id,
                UpdateProjectDraftRequest(title="New Title"),
            )
        self.assertIn("Published", str(ctx.exception))

    def test_publish_global_write_failure_rolls_back(self):
        ps = self._create_project("proj-draft")
        svc = self._service(ps)
        self._create_card_with_runtime("proj-draft", "card-001", "Clean Card")

        created = svc.create_project_draft("proj-draft", "card-001")
        svc.review_project_draft("proj-draft", created.draft_id)

        original_add = svc._add_to_index
        failing_id: list[str] = []

        def _failing_add(bp):
            failing_id.append(bp.blueprint_id)
            raise RuntimeError("index write failed")

        svc._add_to_index = _failing_add
        with self.assertRaises(ValueError) as ctx:
            svc.publish_project_draft("proj-draft", created.draft_id)
        self.assertIn("Failed to write global blueprint", str(ctx.exception))

        # Restore for verification.
        svc._add_to_index = original_add

        # No orphan blueprint directory and no index entry.
        self.assertEqual(len(failing_id), 1)
        orphan_dir = svc._blueprint_dir(failing_id[0])
        self.assertFalse(orphan_dir.exists())
        entries = svc.list_blueprints()
        self.assertNotIn(failing_id[0], [e.get("blueprint_id") for e in entries])

        # Project draft should still be approved, not published.
        draft = svc.get_project_draft("proj-draft", created.draft_id)
        self.assertEqual(draft["status"], "approved")


# ======================================================================
# Review pipeline (generalize-first + lock boundary + fallback)
# ======================================================================


def _make_generalization_scrubbing_project_name(project_name: str) -> GeneralizedBlueprint:
    """A successful generalization that scrubs the project name and keeps the
    required input count (so the structural guard passes)."""
    return GeneralizedBlueprint(
        title="Generic count matrix QC",
        summary="Quality control for a count matrix.",
        tags=["qc"],
        domain="scrna",
        use_cases=["QC a raw count matrix."],
        inputs_schema=[{"slot": "count_matrix", "label": "count matrix", "accepted_formats": ["csv"], "required": True}],
        outputs_schema=[{"role": "qc_figure", "label": "QC figure", "artifact_class": "figure", "required": True}],
        instruction_blocks=["Read the count matrix and plot QC."],
        confidence="high",
    )


class TestReviewPipeline(_Base):
    def _card_with_project_name(self, project_service, project_id="proj-rev"):
        store = project_service.graph_store(project_id)
        card = Card(
            card_id="card-rev",
            card_type="module",
            title="Test Project count matrix QC",
            status="proposed",
            summary="analysis at /home/user/oaa",
            inputs=[CardAssetRef(label="count matrix txt", asset_id=None)],
            outputs=[CardOutputSpec(role="qc_figure", label="QC figure", artifact_class="figure")],
            executor_context=ExecutorContext(runtime_bindings=RuntimeBindings(conda_env="scanpy-env")),
        )
        store.save_cards([card])
        return card

    def test_review_without_generalization_falls_back_to_rule(self):
        # No manager key => generalize() returns None => rule-only on the original
        # blueprint. Title still contains the project name => rule review fails.
        ps = self._create_project("proj-rev")
        svc = self._service(ps)
        self._card_with_project_name(ps)
        created = svc.create_project_draft("proj-rev", "card-rev")

        result = svc.review_project_draft("proj-rev", created.draft_id)
        self.assertEqual(result["status"], "rejected")
        fields = [i.get("field") for i in result["review"]["issues"]]
        self.assertIn("generalization", fields)  # fallback info issue present

    def test_review_generalizes_first_then_approves(self):
        ps = self._create_project("proj-rev")
        svc = self._service(ps)
        self._card_with_project_name(ps)
        created = svc.create_project_draft("proj-rev", "card-rev")

        with patch(
            "app.services.card_library_service.CardDesensitizationService.generalize",
            return_value=_make_generalization_scrubbing_project_name("Test Project"),
        ):
            result = svc.review_project_draft("proj-rev", created.draft_id)
        # Generalized candidate has no project name => rule review passes => approved.
        self.assertEqual(result["status"], "approved")
        # Blueprint on disk is the generalized one.
        draft = svc.get_project_draft("proj-rev", created.draft_id)
        self.assertEqual(draft["blueprint"]["title"], "Generic count matrix QC")
        self.assertTrue(draft["blueprint"]["use_cases"])

    def test_review_rejects_concurrent_modification(self):
        ps = self._create_project("proj-rev")
        svc = self._service(ps)
        self._card_with_project_name(ps)
        created = svc.create_project_draft("proj-rev", "card-rev")

        from fastapi import HTTPException

        def side_effect(blueprint, project_name=""):
            # Simulate a concurrent edit during the outside-lock window.
            draft_dir = svc._project_draft_dir("proj-rev", created.draft_id)
            data = svc.get_project_draft("proj-rev", created.draft_id)
            from app.models.card_blueprint import CardBlueprintDraft
            d = CardBlueprintDraft.model_validate(data)
            d.updated_at = "2030-01-01T00:00:00Z"
            from app.services.utils import atomic_write_json
            atomic_write_json(draft_dir / "blueprint.json", d.model_dump())
            return None

        with patch(
            "app.services.card_library_service.CardDesensitizationService.generalize",
            side_effect=side_effect,
        ):
            with self.assertRaises(HTTPException) as ctx:
                svc.review_project_draft("proj-rev", created.draft_id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_review_guard_blocks_dropped_required_inputs(self):
        ps = self._create_project("proj-rev")
        svc = self._service(ps)
        self._card_with_project_name(ps)
        created = svc.create_project_draft("proj-rev", "card-rev")

        # Generalization that DROPS the required input => structural guard keeps
        # the original blueprint (which still has the project name => rejected).
        dropped = GeneralizedBlueprint(
            title="Generic",
            summary="x",
            inputs_schema=[],  # dropped the required input
            outputs_schema=[],
            instruction_blocks=[],
            confidence="high",
        )
        with patch(
            "app.services.card_library_service.CardDesensitizationService.generalize",
            return_value=dropped,
        ):
            result = svc.review_project_draft("proj-rev", created.draft_id)
        self.assertEqual(result["status"], "rejected")
        draft = svc.get_project_draft("proj-rev", created.draft_id)
        # Original title retained (not overwritten by the guard-failing gen).
        self.assertEqual(draft["blueprint"]["title"], "Test Project count matrix QC")


# ======================================================================
# Reference-data dependencies at instantiation
# ======================================================================


class TestInstantiateReferenceAssets(_Base):
    def _blueprint_with_reference(self, svc: CardLibraryService, ref_id: str) -> str:
        bp = {
            "blueprint_id": "bp-ref",
            "title": "Annotate",
            "summary": "annotate counts",
            "inputs_schema": [{"slot": "counts", "label": "counts", "accepted_formats": ["csv"], "required": True}],
            "outputs_schema": [{"role": "annotated", "label": "annotated", "artifact_class": "table", "required": True}],
            "reference_assets": [{"ref_id": ref_id, "role": "gene_annotation", "required": True}],
        }
        result = svc.save_from_import(bp)
        return result.blueprint_id

    def test_instantiate_resolves_reference_paths(self):
        ps = self._create_project("proj-ref")
        svc = self._service(ps)

        ref_src = self.data_root / "genes.gtf"
        ref_src.write_bytes(b"geneA\n")
        ref_meta = ReferenceDataService(settings=self.settings).register_local(ref_src, name="genes", kind="gtf")

        blueprint_id = self._blueprint_with_reference(svc, ref_meta.ref_id)
        self._add_asset("proj-ref", "sha256:" + "c" * 64, path="results/counts.csv")

        result = svc.instantiate(
            blueprint_id,
            "proj-ref",
            InstantiateRequest(input_bindings={"counts": "sha256:" + "c" * 64}),
        )
        self.assertEqual(result.blockers, [])
        self.assertTrue(result.card_id)

        store = ps.graph_store("proj-ref")
        card = next(c for c in store.load_cards() if c.card_id == result.card_id)
        ref_paths = [r.path for r in card.executor_context.references]
        self.assertTrue(any("genes.gtf" in p for p in ref_paths), ref_paths)
        self.assertEqual(
            card.executor_context.template_metadata.get("reference_paths"),
            {"gene_annotation": ref_paths[0]},
        )

    def test_instantiate_blocks_missing_required_reference(self):
        ps = self._create_project("proj-ref")
        svc = self._service(ps)
        blueprint_id = self._blueprint_with_reference(svc, "ref_doesnotexist")
        self._add_asset("proj-ref", "sha256:" + "d" * 64, path="results/counts.csv")

        result = svc.instantiate(
            blueprint_id,
            "proj-ref",
            InstantiateRequest(input_bindings={"counts": "sha256:" + "d" * 64}),
        )
        self.assertTrue(any("gene_annotation" in b for b in result.blockers), result.blockers)
        self.assertEqual(result.card_id, "")


# ======================================================================
# Reference-data usage scanning
# ======================================================================


class TestReferenceUsage(_Base):
    def test_reference_usage_scans_global_blueprints_and_project_drafts(self):
        ps = self._create_project("proj-usage")
        svc = self._service(ps)

        ref_src = self.data_root / "genes.gtf"
        ref_src.write_bytes(b"geneA\n")
        ref_meta = ReferenceDataService(settings=self.settings).register_local(ref_src, name="genes", kind="gtf")

        # Global blueprint with the reference
        svc.save_from_import({
            "blueprint_id": "bp-used",
            "title": "Used BP",
            "reference_assets": [{"ref_id": ref_meta.ref_id, "role": "gene_annotation", "required": True}],
        })

        # Project draft with the reference
        self._create_card_with_runtime("proj-usage", "card-001", "Draft Card")
        created = svc.create_project_draft("proj-usage", "card-001")
        draft = svc.get_project_draft("proj-usage", created.draft_id)
        draft["blueprint"]["reference_assets"] = [
            {"ref_id": ref_meta.ref_id, "role": "gene_annotation", "required": True}
        ]
        from app.services.utils import atomic_write_json
        draft_dir = ps.project_path("proj-usage") / "card-library-drafts" / "drafts" / created.draft_id
        atomic_write_json(draft_dir / "blueprint.json", draft)

        usages = svc.reference_usage(ref_meta.ref_id)
        self.assertEqual(len(usages), 2)
        types = {u["type"] for u in usages}
        self.assertEqual(types, {"blueprint", "draft"})

    def test_reference_usage_empty_when_unused(self):
        svc = self._service()
        svc.save_from_import({"blueprint_id": "bp-unused", "title": "Unused BP"})
        self.assertEqual(svc.reference_usage("ref_doesnotexist"), [])


if __name__ == "__main__":
    unittest.main()
