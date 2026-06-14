from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


PackageImportStatus = Literal["ready", "ready_with_warnings", "blocked"]


class PackageCompatibility(BaseModel):
    supported_runtimes: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    optional_skills: list[str] = Field(default_factory=list)
    required_mcps: list[str] = Field(default_factory=list)
    optional_mcps: list[str] = Field(default_factory=list)


class PackageInputSchema(BaseModel):
    slot: str
    label: str
    accepted_formats: list[str] = Field(default_factory=list)
    required: bool = True
    description: str | None = None


class PackageOutputSchema(BaseModel):
    role: str
    artifact_class: str | None = None
    accepted_formats: list[str] = Field(default_factory=list)
    required: bool = True
    description: str | None = None


class PackageParameter(BaseModel):
    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str | None = None


class PackageRuntimeRequirements(BaseModel):
    python_runtime: str | None = None
    r_runtime: str | None = None


class PackageDependency(BaseModel):
    """A declared dependency that must be satisfied in the target runtime."""

    family: str = "python"  # "python" | "r"
    packages: list[str] = Field(default_factory=list)
    source: str | None = None  # "conda" | "pip" | "cran" | "bioconductor" | None (auto-detect)


class PackageExecutor(BaseModel):
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    script_preference: str | None = None
    runtime_requirements: PackageRuntimeRequirements = Field(default_factory=PackageRuntimeRequirements)
    instruction_blocks: list[str] = Field(default_factory=list)


class PackageBundleFile(BaseModel):
    path: str
    description: str | None = None


class PackageBundle(BaseModel):
    files: list[PackageBundleFile] = Field(default_factory=list)


class PackageProvenance(BaseModel):
    source_template_id: str | None = None
    created_at: str | None = None
    created_by: str | None = None
    content_hash: str | None = None


class PackageManifest(BaseModel):
    schema_version: str = "portable_card_package.v1"
    package_id: str
    version: str = "1.0.0"
    title: str
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    compatibility: PackageCompatibility = Field(default_factory=PackageCompatibility)
    inputs_schema: list[PackageInputSchema] = Field(default_factory=list)
    outputs_schema: list[PackageOutputSchema] = Field(default_factory=list)
    parameters: list[PackageParameter] = Field(default_factory=list)
    executor: PackageExecutor = Field(default_factory=PackageExecutor)
    bundle: PackageBundle = Field(default_factory=PackageBundle)
    provenance: PackageProvenance = Field(default_factory=PackageProvenance)
    dependencies: list[PackageDependency] = Field(default_factory=list)
    """Optional: declared package-level dependencies that must be satisfied in the target runtime."""


class PortableCardPackage(BaseModel):
    """A portable method package that can be imported and instantiated into a project card."""

    manifest: PackageManifest
    bundle_files: dict[str, str] = Field(default_factory=dict)
    """Map of bundle file path -> file content (text only for v1)."""


class PackageIndexEntry(BaseModel):
    """Lightweight index entry for fast search/list."""

    package_id: str
    version: str
    title: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    compatibility: PackageCompatibility = Field(default_factory=PackageCompatibility)
    import_status: PackageImportStatus = "ready"


class PackageImportResult(BaseModel):
    """Result of importing a portable card package."""

    status: PackageImportStatus
    package_id: str
    version: str
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class PackageInstantiationResult(BaseModel):
    """Result of instantiating a package into a project card."""

    card_id: str
    project_id: str
    package_id: str
    version: str
    effective_python_runtime: str | None = None
    effective_r_runtime: str | None = None
    runtime_source: str | None = None
    python_runtime_source: str | None = None
    r_runtime_source: str | None = None
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
