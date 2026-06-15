"""Card desensitization / generalization agent.

Turns a project-specific draft blueprint into a generic, reusable one: rewrites
input/output labels to generic domain terms, authors ``use_cases`` for manager
retrieval, scrubs residual project-specific leakage (project name, paths, asset
ids), and keeps the technical content intact. Reuses the manager agent's LLM
configuration (provider/model/auth) but runs as an independent one-shot call
during draft review.

The call is best-effort: any failure (missing key, network, non-conforming
schema) returns ``None`` so the review pipeline can fall back to rule-only
review without ever raising to the caller.
"""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib import error, request

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.models.card_blueprint import (
    BlueprintInputSchema,
    BlueprintOutputSchema,
    CardBlueprint,
)
from app.services.manager_planner import DeepSeekManagerPlanner
from app.services.provider_errors import (
    provider_error_from_exception,
    provider_error_from_http_error,
    provider_error_from_url_error,
    provider_invalid_response_error,
    retry_provider_call,
)

TOOL_NAME = "submit_generalized_blueprint"

# One-shot structured output can be sizable (full instruction_blocks), so allow
# more headroom than the manager chat default.
_MAX_TOKENS = 8192

DESENSITIZATION_SYSTEM_PROMPT = """You are a card generalization agent for Blueprint RE, a bioinformatics platform.

You receive a draft "analysis card" extracted from one specific project. Your job is to rewrite it into a GENERIC, REUSABLE, PROJECT-AGNOSTIC card that other projects (and the manager AI) can retrieve and reuse.

Rules:
- Remove ALL project-specific information: project names, patient/sample identifiers, absolute file paths, asset ids (sha256:...), usernames, institution names, secrets/tokens.
- Generalize input/output labels and slots to generic domain terms. For example "oaa_count_matrix_txt" -> "count_matrix"; "PatientB_rep1.h5ad" style references -> "expression_matrix". Keep accepted_formats/artifact_class technical and correct.
- Preserve the analytical intent and runtime/skill requirements; do not invent new capabilities.
- Author 2-5 concise "use_cases" describing when this card is the right tool (these drive retrieval).
- Rewrite title/summary to be clear and generic.
- Keep instruction_blocks as actionable steps but scrub any specifics; they may be brief.
- Set "confidence": "high" | "medium" | "low" reflecting how cleanly you could generalize, and put any concerns in "notes".

Return the result by calling the submit_generalized_blueprint tool. Do not output prose."""

DesensitizationConfidence = Literal["high", "medium", "low"]


class GeneralizedBlueprint(BaseModel):
    """The agent's generalized, project-agnostic version of a draft blueprint."""

    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    domain: str = ""
    use_cases: list[str] = Field(default_factory=list)
    inputs_schema: list[BlueprintInputSchema] = Field(default_factory=list)
    outputs_schema: list[BlueprintOutputSchema] = Field(default_factory=list)
    instruction_blocks: list[str] = Field(default_factory=list)
    confidence: DesensitizationConfidence = "medium"
    notes: str | None = None


class CardDesensitizationService:
    """Independent one-shot LLM service that reuses the manager LLM config."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def generalize(self, blueprint: CardBlueprint, project_name: str = "") -> GeneralizedBlueprint | None:
        """Best-effort generalization. Returns None on any failure."""
        try:
            api_key = self._api_key()
            if not api_key:
                return None
            payload = self._build_payload(blueprint, project_name)
            response = self._post_messages(payload, api_key)
            tool_input = DeepSeekManagerPlanner._extract_named_tool_input(response, TOOL_NAME)
            return GeneralizedBlueprint.model_validate(tool_input)
        except Exception:
            # Best-effort: never propagate to the review pipeline.
            return None

    # ------------------------------------------------------------------
    # Config + payload
    # ------------------------------------------------------------------

    def _api_key(self) -> str:
        value = self.settings.manager_api_key or self.settings.deepseek_api_key
        return value.get_secret_value() if value else ""

    def _base_url(self) -> str:
        return self.settings.manager_api_base_url or self.settings.deepseek_api_base_url

    def _build_payload(self, blueprint: CardBlueprint, project_name: str) -> dict[str, Any]:
        source = {
            "project_name_to_scrub": project_name or "",
            "title": blueprint.title,
            "summary": blueprint.summary,
            "tags": blueprint.tags,
            "domain": blueprint.domain,
            "inputs_schema": [item.model_dump() for item in blueprint.inputs_schema],
            "outputs_schema": [item.model_dump() for item in blueprint.outputs_schema],
            "instruction_blocks": blueprint.instruction_blocks,
            "runtime_requirements": blueprint.runtime_requirements.model_dump(),
            "skills": blueprint.skills,
            "mcp_servers": blueprint.mcp_servers,
        }
        return {
            "model": self._resolved_model(),
            "max_tokens": _MAX_TOKENS,
            "temperature": self.settings.manager_temperature,
            "system": DESENSITIZATION_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Generalize this draft analysis card into a reusable, project-agnostic card:\n"
                            + json.dumps(source, ensure_ascii=False, indent=2),
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Return the generalized, reusable analysis card as structured data.",
                    "input_schema": GeneralizedBlueprint.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "any"},
        }

    def _resolved_model(self) -> str:
        return DeepSeekManagerPlanner.resolve_tool_model(self.settings.manager_model)

    # ------------------------------------------------------------------
    # HTTP (mirrors manager_planner, converged for one-shot use)
    # ------------------------------------------------------------------

    def _post_messages(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        configured_model = self.settings.manager_model
        resolved_model = self._resolved_model()
        endpoint = f"{self._base_url().rstrip('/')}/v1/messages"
        http_request = request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )

        def _send() -> str:
            try:
                with request.urlopen(http_request, timeout=min(self.settings.manager_timeout_seconds, 90)) as response:
                    return response.read().decode("utf-8")
            except error.HTTPError as exc:
                raise provider_error_from_http_error(
                    exc,
                    provider="deepseek",
                    role="desensitization",
                    configured_model=configured_model,
                    resolved_model=resolved_model,
                ) from exc
            except error.URLError as exc:
                raise provider_error_from_url_error(exc, provider="deepseek", role="desensitization") from exc
            except (TimeoutError, OSError) as exc:
                raise provider_error_from_exception(exc, provider="deepseek", role="desensitization") from exc

        raw = retry_provider_call(_send, max_attempts=3)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise provider_invalid_response_error(
                exc,
                provider="deepseek",
                role="desensitization",
                message="Desensitization model returned invalid JSON at the HTTP layer.",
                detail=raw[:1200],
            ) from exc
