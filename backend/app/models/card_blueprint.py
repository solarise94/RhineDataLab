from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.output_contracts import ArtifactClass, normalize_output_format


# ---------------------------------------------------------------------------
# Blueprint sub-schemas
# ---------------------------------------------------------------------------


class BlueprintInputSchema(BaseModel):
    """Slot declaration for a blueprint's expected input."""

    slot: str
    label: str
    accepted_formats: list[str] = Field(default_factory=list)
    required: bool = True
    description: str | None = None

    @field_validator("slot")
    @classmethod
    def _validate_slot(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
        if not normalized:
            raise ValueError("input slot is required.")
        return normalized

    @field_validator("accepted_formats")
    @classmethod
    def _normalize_accepted_formats(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values or []:
            item = normalize_output_format(value)
            if item and item not in normalized:
                normalized.append(item)
        return normalized


class BlueprintOutputSchema(BaseModel):
    """Output contract declaration, mirrors OutputContractBase fields."""

    role: str
    label: str
    artifact_class: ArtifactClass = "figure"
    accepted_formats: list[str] = Field(default_factory=list)
    preferred_format: str | None = None
    required: bool = True
    description: str | None = None

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
        if not normalized:
            raise ValueError("output role is required.")
        return normalized

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("output label is required.")
        return normalized

    @field_validator("accepted_formats")
    @classmethod
    def _normalize_formats(cls, values: list[str]) -> list[str]:
        seen: list[str] = []
        for v in values or []:
            item = normalize_output_format(v)
            if item and item not in seen:
                seen.append(item)
        return seen

    @field_validator("preferred_format")
    @classmethod
    def _normalize_preferred_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_output_format(value)
        return normalized or None

    @model_validator(mode="after")
    def _validate_preferred(self) -> BlueprintOutputSchema:
        if self.preferred_format and self.accepted_formats and self.preferred_format not in self.accepted_formats:
            raise ValueError("preferred_format must be one of accepted_formats.")
        return self


class BlueprintParameter(BaseModel):
    """A user-fillable parameter for the blueprint."""

    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
        if not normalized:
            raise ValueError("parameter name is required.")
        return normalized


class BlueprintRuntimeRequirement(BaseModel):
    env_hint: str = ""
    packages: list[str] = Field(default_factory=list)


class BlueprintRuntimeRequirements(BaseModel):
    python: BlueprintRuntimeRequirement | Literal["__system__"] = Field(
        default_factory=BlueprintRuntimeRequirement,
    )
    r: BlueprintRuntimeRequirement | Literal["__system__"] = "__system__"


class BlueprintProvenance(BaseModel):
    source_card_id: str | None = None
    source_project_id: str | None = None
    created_at: str | None = None
    created_by: str = "user"
    last_used_at: str | None = None
    use_count: int = 0


# ---------------------------------------------------------------------------
# Core model
# ---------------------------------------------------------------------------


class CardBlueprint(BaseModel):
    """A reusable card configuration blueprint stored in the card library."""

    blueprint_id: str
    version: str = "1.0.0"
    schema_version: str = "card_blueprint.v1"
    title: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    domain: str = ""
    cover_art: str | None = None

    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)

    runtime_requirements: BlueprintRuntimeRequirements = Field(
        default_factory=BlueprintRuntimeRequirements,
    )

    inputs_schema: list[BlueprintInputSchema] = Field(default_factory=list)
    outputs_schema: list[BlueprintOutputSchema] = Field(default_factory=list)
    parameters: list[BlueprintParameter] = Field(default_factory=list)
    instruction_blocks: list[str] = Field(default_factory=list)

    provenance: BlueprintProvenance = Field(default_factory=BlueprintProvenance)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class CardBlueprintIndexEntry(BaseModel):
    """Lightweight entry in the card library index."""

    blueprint_id: str
    title: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    domain: str = ""
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    runtime_hints: list[str] = Field(default_factory=list)
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str | None = None


class CardBlueprintIndex(BaseModel):
    schema_version: str = "card_library_index.v1"
    entries: list[CardBlueprintIndexEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------


class SaveFromCardRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    card_id: str = Field(..., min_length=1)


class UpdateBlueprintRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    domain: str | None = None


class InstantiateRequest(BaseModel):
    input_bindings: dict[str, str] = Field(default_factory=dict)
    python_runtime: str | None = None
    r_runtime: str | None = None
    parameter_values: dict[str, Any] = Field(default_factory=dict)


class SaveResult(BaseModel):
    blueprint_id: str
    warnings: list[str] = Field(default_factory=list)


class InstantiateResult(BaseModel):
    card_id: str
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Project draft flow models
# ---------------------------------------------------------------------------


DraftStatus = Literal["draft", "needs_review", "approved", "rejected", "published"]


class BlueprintReviewIssue(BaseModel):
    severity: Literal["info", "warning", "error"]
    field: str
    message: str
    suggested_value: str | None = None


class BlueprintReviewResult(BaseModel):
    verdict: Literal["pass", "warn", "fail"]
    summary: str
    issues: list[BlueprintReviewIssue] = Field(default_factory=list)


class CardBlueprintDraft(BaseModel):
    draft_id: str
    status: DraftStatus
    blueprint: CardBlueprint
    review: BlueprintReviewResult | None = None
    global_blueprint_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CardBlueprintDraftIndexEntry(BaseModel):
    draft_id: str
    status: DraftStatus
    global_blueprint_id: str | None = None
    title: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    domain: str = ""
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    runtime_hints: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class CreateProjectDraftRequest(BaseModel):
    card_id: str


class CreateProjectDraftResponse(BaseModel):
    draft_id: str
    warnings: list[str] = Field(default_factory=list)


class ProjectDraftListResponse(BaseModel):
    entries: list[CardBlueprintDraftIndexEntry]


class ProjectDraftResponse(BaseModel):
    draft: CardBlueprintDraft


class PublishDraftResponse(BaseModel):
    draft_id: str
    global_blueprint_id: str


class UpdateProjectDraftRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    domain: str | None = None
    instruction_blocks: list[str] | None = None
    python_packages: list[str] | None = None
    r_packages: list[str] | None = None
