"""Tests for the card desensitization / generalization agent (LLM mocked)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.models.card_blueprint import (
    BlueprintInputSchema,
    BlueprintOutputSchema,
    CardBlueprint,
)
from app.services.card_desensitization_service import (
    CardDesensitizationService,
    GeneralizedBlueprint,
)


def _blueprint() -> CardBlueprint:
    return CardBlueprint(
        blueprint_id="bp-1",
        title="oaa count matrix QC",
        summary="/home/user/oaa analysis of patientB",
        domain="scrna",
        inputs_schema=[BlueprintInputSchema(slot="count_matrix_txt", label="oaa_count_matrix_txt", accepted_formats=["csv"])],
        outputs_schema=[BlueprintOutputSchema(role="qc_figure", label="QC figure", artifact_class="figure")],
        instruction_blocks=["Read /home/user/oaa/counts.csv and plot QC."],
    )


def _tool_response(payload: dict) -> dict:
    return {"content": [{"type": "tool_use", "name": "submit_generalized_blueprint", "input": payload}]}


def _generalized_payload() -> dict:
    return {
        "title": "Single-cell count matrix QC",
        "summary": "Quality control for a single-cell count matrix.",
        "tags": ["qc", "scrna"],
        "domain": "scrna",
        "use_cases": ["QC a raw scRNA count matrix before normalization."],
        "inputs_schema": [{"slot": "count_matrix", "label": "count matrix", "accepted_formats": ["csv"], "required": True}],
        "outputs_schema": [{"role": "qc_figure", "label": "QC figure", "artifact_class": "figure", "required": True}],
        "instruction_blocks": ["Read the count matrix and plot QC metrics."],
        "confidence": "high",
        "notes": "generalized cleanly",
    }


class TestCardDesensitization(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(manager_api_key="test-key")

    def test_generalize_returns_structured_output(self):
        svc = CardDesensitizationService(settings=self.settings)
        with patch.object(CardDesensitizationService, "_post_messages", return_value=_tool_response(_generalized_payload())):
            gen = svc.generalize(_blueprint(), project_name="oaa")
        self.assertIsInstance(gen, GeneralizedBlueprint)
        self.assertEqual(gen.title, "Single-cell count matrix QC")
        self.assertEqual(gen.use_cases[0][:2], "QC")
        self.assertEqual(gen.confidence, "high")

    def test_generalize_returns_none_on_http_failure(self):
        svc = CardDesensitizationService(settings=self.settings)

        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        with patch.object(CardDesensitizationService, "_post_messages", side_effect=boom):
            self.assertIsNone(svc.generalize(_blueprint()))

    def test_generalize_returns_none_when_tool_call_missing(self):
        svc = CardDesensitizationService(settings=self.settings)
        with patch.object(CardDesensitizationService, "_post_messages", return_value={"content": [{"type": "text", "text": "no tool"}]}):
            self.assertIsNone(svc.generalize(_blueprint()))

    def test_generalize_returns_none_without_api_key(self):
        svc = CardDesensitizationService(settings=Settings(manager_api_key="x"))
        with patch.object(CardDesensitizationService, "_api_key", return_value=""), \
             patch.object(CardDesensitizationService, "_post_messages") as mocked:
            self.assertIsNone(svc.generalize(_blueprint()))
            mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
