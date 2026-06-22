"""Tests for plan-revision status handling (#6 / audit §2.1).

Revising a card's plan (step/inputs/outputs) sends it back to ``planned`` so the
stale prior run is no longer treated as current. Historically that reset was
*silent*: a ``needs_review`` / ``accepted`` card was quietly coerced to
``planned`` with no signal to the caller, so the Manager-AI never learned its
pending review or acceptance had been discarded.

The fix surfaces the coerce explicitly without changing the (correct) reset
behavior:
- ``_apply_plan_revision_status`` returns ``(card, warnings)`` where ``warnings``
  is a structured ``plan_revision_warnings`` list, empty unless a coerce happened.
- The audit note is *appended* to ``manager_review`` (annotate's append idiom),
  never clobbering existing human/AI review text, and never duplicated.
- ``running`` / ``reviewing`` are left untouched (a live run owns the card) and
  are intentionally NOT treated as a coerce — that asymmetry vs the 5-element
  ACTIVE_RUN_STATUSES is a state-machine concern out of #6 scope (docs/67 §2.1).
- ``update_card`` threads the warnings into its result as ``plan_revision_warnings``.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings, get_settings
from app.models.cards import Card
from app.services.manager_blueprint_tools import ManagerBlueprintTools
from app.services.project_service import ProjectService


def _card(status: str, *, manager_review: str = "", step: int = 1) -> Card:
    return Card(
        card_id="c1",
        card_type="run",
        title="T",
        status=status,
        summary="s",
        inputs=[],
        outputs=[],
        step=step,
        manager_review=manager_review,
        progress_note="mid-run note",
    )


class ApplyPlanRevisionStatusTest(unittest.TestCase):
    """Direct unit tests of the zero-coverage staticmethod."""

    def _apply(self, previous: Card, updated: Card | None = None):
        # update_card inherits status from the existing card, so by the time the
        # status resolver runs, updated.status == previous.status. Mirror that.
        updated = updated if updated is not None else previous.model_copy(deep=True)
        return ManagerBlueprintTools._apply_plan_revision_status(previous, updated)

    def test_already_planned_stays_planned_no_warning(self):
        card, warnings = self._apply(_card("planned"))
        self.assertEqual(card.status, "planned")
        self.assertEqual(warnings, [])
        self.assertIsNone(card.progress_note)  # stale progress is cleared
        self.assertEqual(card.manager_review, "")  # no note for a non-coerce

    def test_running_is_kept_untouched_no_warning(self):
        card, warnings = self._apply(_card("running", manager_review="live"))
        self.assertEqual(card.status, "running")
        self.assertEqual(warnings, [])
        # A live run owns the card: progress_note and review are left intact.
        self.assertEqual(card.progress_note, "mid-run note")
        self.assertEqual(card.manager_review, "live")

    def test_reviewing_is_kept_untouched_no_warning(self):
        card, warnings = self._apply(_card("reviewing"))
        self.assertEqual(card.status, "reviewing")
        self.assertEqual(warnings, [])

    def test_needs_review_is_coerced_with_structured_warning(self):
        card, warnings = self._apply(_card("needs_review"))
        self.assertEqual(card.status, "planned")
        self.assertIsNone(card.progress_note)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["previous_status"], "needs_review")
        self.assertEqual(warnings[0]["coerced_to"], "planned")
        self.assertIn("plan revised", warnings[0]["reason"])

    def test_accepted_coerce_appends_note_preserving_existing_review(self):
        card, warnings = self._apply(_card("accepted", manager_review="prior human note"))
        self.assertEqual(card.status, "planned")
        self.assertTrue(warnings)
        # Existing review preserved; audit note appended, not overwritten.
        self.assertTrue(card.manager_review.startswith("prior human note"))
        self.assertIn("revised from previous status accepted", card.manager_review)

    def test_coerce_note_is_not_duplicated_on_repeated_revision(self):
        # NOTE: this is a *direct* unit test of _apply_plan_revision_status, so it
        # sets updated.status by hand. It deliberately does NOT model the update_card
        # path, where updated.status always equals previous.status (the payload is
        # copied from the existing card in _normalize_update_card_payload). Here we
        # exercise _apply's own append-idempotency across two distinct prior statuses.
        # First revision coerces accepted -> planned and appends the note.
        first, _ = self._apply(_card("accepted", manager_review="prior"))
        appended = first.manager_review
        # A subsequent revision of a now-stale card (e.g. rejected) must not append
        # the *same* accepted-note again, but a different prior status still appends.
        again = Card(**{**first.model_dump(), "status": "rejected", "manager_review": appended})
        card, warnings = self._apply(again)
        self.assertTrue(warnings)
        # Old accepted-note still present exactly once, new rejected-note appended once.
        self.assertEqual(card.manager_review.count("revised from previous status accepted"), 1)
        self.assertEqual(card.manager_review.count("revised from previous status rejected"), 1)

    def test_same_note_present_is_not_appended_twice(self):
        note = "Card plan revised from previous status needs_review; awaiting a new run."
        card, _ = self._apply(_card("needs_review", manager_review=note))
        self.assertEqual(card.manager_review.count(note), 1)


class UpdateCardPlanRevisionWiringTest(unittest.TestCase):
    """End-to-end: update_card surfaces plan_revision_warnings in its result."""

    def setUp(self):
        self.data_root = Path(tempfile.mkdtemp())
        self.settings = Settings(data_root=self.data_root)
        get_settings.cache_clear()
        with patch("app.services.project_service.get_settings", return_value=self.settings):
            self.ps = ProjectService()
        self.ps.create_project(project_id="p1", name="T", current_goal="g")
        self.tools = ManagerBlueprintTools(self.ps)

    def tearDown(self):
        get_settings.cache_clear()
        shutil.rmtree(self.data_root, ignore_errors=True)

    def _seed(self, status: str, *, manager_review: str = ""):
        self.ps.graph_store("p1").save_cards([_card(status, manager_review=manager_review)])

    def test_coerce_surfaces_warning_and_appends_review(self):
        self._seed("needs_review", manager_review="prior human note")
        res = self.tools.update_card("p1", {"card_id": "c1", "step": 2})
        self.assertEqual(res["card"]["status"], "planned")
        self.assertIn("plan_revision_warnings", res)
        self.assertEqual(res["plan_revision_warnings"][0]["previous_status"], "needs_review")
        self.assertTrue(res["card"]["manager_review"].startswith("prior human note"))
        self.assertIn("awaiting a new run", res["card"]["manager_review"])

    def test_planned_card_has_no_warning_field(self):
        self._seed("planned")
        res = self.tools.update_card("p1", {"card_id": "c1", "step": 2})
        self.assertEqual(res["card"]["status"], "planned")
        self.assertNotIn("plan_revision_warnings", res)

    def test_running_card_is_kept_and_has_no_warning_field(self):
        self._seed("running")
        res = self.tools.update_card("p1", {"card_id": "c1", "step": 2})
        self.assertEqual(res["card"]["status"], "running")
        self.assertNotIn("plan_revision_warnings", res)


if __name__ == "__main__":
    unittest.main()
