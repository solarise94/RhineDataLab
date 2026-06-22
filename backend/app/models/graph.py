from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.cards import CardStatus


ModuleType = Literal["analysis_module", "module_group"]
AssetStatus = Literal["candidate", "valid", "stale", "superseded", "rejected", "archived", "missing"]
RunStatus = Literal["queued", "launching", "running", "reviewing", "needs_approval", "success", "failed", "cancelled", "reviewed"]
ClaimStatus = Literal["candidate", "valid", "stale", "superseded", "rejected", "archived", "missing"]


# --- RunStatus domain status-sets (single source of truth) ---------------------
# Co-located with the RunStatus Literal so the canonical type and the sets derived
# from it stay adjacent. Centralizing these eliminates the literal copies that had
# already drifted out of sync across services (see docs/67 §5.1).

# A run that is still "in flight" — occupying an execution slot / not yet terminal.
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset(
    {"queued", "launching", "needs_approval", "running", "reviewing"}
)

# The subset of active runs that a backend restart strands as ghosts: their
# progress depends on an in-memory thread/process that is gone after restart and
# there is no re-dispatch path, so reconcile must mark them ``failed``.
#
# = ACTIVE_RUN_STATUSES − {"needs_approval"}. ``needs_approval`` is deliberately
# EXCLUDED: it is a pre-launch permission gate (worker_service.start_run sets it at
# creation when there are unresolved approvals) with no process and no thread, and
# it is resumable from disk via continue_run after a restart. Marking it ``failed``
# during reconcile would kill every run that is legitimately paused waiting for the
# user. ``queued`` IS included: start_run reserves the slot and starts the worker
# thread inline, so a thread-less ``queued`` run after restart is a genuine orphan.
# This set is intentionally named distinctly from ACTIVE_RUN_STATUSES so callers do
# not accidentally feed the full active set into reconcile.
RESTART_ORPHANED_RUN_STATUSES: frozenset[str] = frozenset(
    {"queued", "launching", "running", "reviewing"}
)

# Terminal run statuses: the run has finished and will not advance further.
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"success", "failed", "cancelled", "reviewed"}
)


# --- AssetStatus domain status-sets (single source of truth) -------------------
# Asset statuses that count as a usable input/output. Previously duplicated under
# four different local names (VALID_LAUNCHABLE_INPUT_STATUSES / _VALID_INPUT_STATUSES
# / VALID_INPUT_STATUSES / VALID_INPUT_ASSET_STATUSES) plus bare literals.
VALID_INPUT_ASSET_STATUSES: frozenset[str] = frozenset({"valid", "candidate"})

# Preference ordering when several concrete assets compete for the same logical
# slot (lower = preferred). Previously copied verbatim in three services.
ASSET_STATUS_RANK: dict[str, int] = {
    "valid": 0,
    "candidate": 1,
    "stale": 2,
    "superseded": 3,
    "rejected": 4,
    "archived": 5,
    "missing": 6,
}


class ModuleRef(BaseModel):
    module_id: str
    title: str
    status: CardStatus


class Module(BaseModel):
    module_id: str
    title: str
    type: ModuleType
    status: CardStatus
    summary: str
    depends_on_assets: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    linked_cards: list[str] = Field(default_factory=list)
    linked_runs: list[str] = Field(default_factory=list)
    submodules: list[ModuleRef] = Field(default_factory=list)
    created_by: str
    created_at: str


class Asset(BaseModel):
    asset_id: str
    asset_type: str
    title: str
    status: AssetStatus
    created_by_run: str | None = None
    path: str
    artifact_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    summary: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    report_selected: bool = False


class Claim(BaseModel):
    claim_id: str
    text: str
    status: ClaimStatus
    depends_on_assets: list[str] = Field(default_factory=list)
    created_by_run: str | None = None
    report_selected: bool = False


class RunRecord(BaseModel):
    run_id: str
    task_id: str | None = None
    card_id: str
    module_id: str | None = None
    status: RunStatus
    title: str
    summary: str
    started_at: str
    finished_at: str | None = None
    worker_type: str = "pi"
    cancel_reason: str | None = None
    archived_at: str | None = None
    cleanup_status: Literal["pending", "completed"] | None = None
    needs_manager_attention: bool = False
    propagate_invalidation: bool = True
    batch_run_id: str | None = None


class ReportItem(BaseModel):
    item_id: str
    section: str
    title: str
    summary: str
    linked_asset_ids: list[str] = Field(default_factory=list)
    linked_claim_ids: list[str] = Field(default_factory=list)


class GraphState(BaseModel):
    modules: list[Module] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)
    report_items: list[ReportItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
