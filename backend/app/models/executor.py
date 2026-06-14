from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ExecutorReference(BaseModel):
    type: Literal["file", "directory", "url", "template"] = "file"
    path: str
    description: str | None = None


class ExecutorToolPolicy(BaseModel):
    network: str = "allow"
    python: bool = True
    rscript: bool = False
    shell: bool = True
    git_write: bool = False


class RuntimeBindings(BaseModel):
    conda_env: str | None = None
    r_env: str | None = None
    container_image: str | None = None
    working_dir: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    runtime_source: str | None = None
    """DEPRECATED: single-field source, kept for backward compat. Prefer python_runtime_source / r_runtime_source."""
    python_runtime_source: str | None = None
    """Source of conda_env: project_default | package_requirement | card_override | __system__"""
    r_runtime_source: str | None = None
    """Source of r_env: project_default | package_requirement | card_override | __system__"""


class ExecutorScriptAssetRequirement(BaseModel):
    requirement_id: str
    label: str
    description: str | None = None
    expected_asset_type: str | None = None
    optional: bool = False


class ExecutorScriptAssetBinding(BaseModel):
    requirement_id: str
    asset_id: str | None = None
    path: str | None = None
    title: str | None = None
    bound_at: str | None = None


class ExecutorContext(BaseModel):
    executor_profile: str | None = None
    executor_profile_id: str | None = None
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    instruction_blocks: list[str] = Field(default_factory=list)
    script_preference: str | None = None
    references: list[ExecutorReference] = Field(default_factory=list)
    tool_policy: ExecutorToolPolicy = Field(default_factory=ExecutorToolPolicy)
    runtime_bindings: RuntimeBindings = Field(default_factory=RuntimeBindings)
    script_asset_requirements: list[ExecutorScriptAssetRequirement] = Field(default_factory=list)
    script_asset_bindings: list[ExecutorScriptAssetBinding] = Field(default_factory=list)
    template_metadata: dict[str, Any] = Field(default_factory=dict)


class ManagerReportingContract(BaseModel):
    transport: Literal["stdout_bp_event", "file_jsonl"] = "stdout_bp_event"
    stdout_prefix: str = "BP_EVENT "
    file_path: str | None = None
    message_types: list[str] = Field(default_factory=lambda: ["progress_update", "issue_report", "final_report"])


class ExecutorStructuredEvent(BaseModel):
    type: Literal["progress_update", "issue_report", "final_report"]
    stage: str | None = None
    progress: int | None = None
    message: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] | None = None
    needs_manager: bool = False
    suggested_actions: list[str] = Field(default_factory=list)
    summary: str | None = None
    key_findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
