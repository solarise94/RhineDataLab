from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


LibraryKind = Literal["skill", "mcp"]


class LibraryEntry(BaseModel):
    id: str
    kind: LibraryKind
    name: str
    aliases: list[str] = Field(default_factory=list)
    summary_short: str
    summary_long: str = ""
    tags: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    source_path: str | None = None
    source_hash: str | None = None
    enabled: bool = True
    runtime_requirements: list[str] = Field(default_factory=list)
    compatibility_notes: list[str] = Field(default_factory=list)
    supported_runtimes: list[str] = Field(default_factory=list)
    launch_hint: str | None = None
    generated_by: str | None = None
    generated_at: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class LibraryRegistry(BaseModel):
    kind: LibraryKind
    items: list[LibraryEntry] = Field(default_factory=list)
    updated_at: str | None = None


class LibrarySearchResult(BaseModel):
    item: LibraryEntry
    score: float
