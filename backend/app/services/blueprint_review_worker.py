from __future__ import annotations

import re

from app.models.card_blueprint import (
    BlueprintReviewIssue,
    BlueprintReviewResult,
    BlueprintRuntimeRequirement,
    CardBlueprint,
)


_PATH_PATTERNS = [
    re.compile(r"/home/[^\s,;)\]}\"]+"),
    re.compile(r"/Users/[^\s,;)\]}\"]+"),
    re.compile(r"C:\\[^\s,;)\]}\"]+"),
]

_ASSET_ID_PATTERN = re.compile(r"\bsha256:[a-f0-9]{64}\b", re.I)

_SECRET_PATTERN = re.compile(r"\b(key|token|password|secret|credential)\b", re.I)


class BlueprintReviewWorker:
    """Rule-based reviewer for card blueprints before global publication."""

    def review(self, blueprint: CardBlueprint, project_name: str = "") -> BlueprintReviewResult:
        issues: list[BlueprintReviewIssue] = []

        searchable_text = " ".join([
            blueprint.title or "",
            blueprint.summary or "",
            " ".join(blueprint.instruction_blocks or []),
        ])

        # Project-specific name leak
        if project_name and project_name.lower() in searchable_text.lower():
            issues.append(BlueprintReviewIssue(
                severity="error",
                field="title/summary/instructions",
                message="检测到项目名称，请移除项目特定信息后再发布。",
            ))

        # Absolute paths
        if any(pat.search(searchable_text) for pat in _PATH_PATTERNS):
            issues.append(BlueprintReviewIssue(
                severity="error",
                field="title/summary/instructions",
                message="检测到绝对路径，请使用相对路径或占位符。",
            ))

        # Asset IDs
        if _ASSET_ID_PATTERN.search(searchable_text):
            issues.append(BlueprintReviewIssue(
                severity="error",
                field="title/summary/instructions",
                message="检测到资产 ID（sha256:...），请移除项目特定引用。",
            ))

        # Secret keywords
        if _SECRET_PATTERN.search(searchable_text):
            issues.append(BlueprintReviewIssue(
                severity="error",
                field="title/summary/instructions",
                message="检测到敏感关键词（key/token/password/secret/credential），请移除或泛化。",
            ))

        # Parameter defaults
        for param in blueprint.parameters:
            default = param.default
            if default is None:
                continue
            value_str = str(default)
            field_name = f"parameters.{param.name}.default"

            if project_name and project_name.lower() in value_str.lower():
                issues.append(BlueprintReviewIssue(
                    severity="error",
                    field=field_name,
                    message="参数默认值包含项目名称，请移除项目特定信息。",
                ))
            if any(pat.search(value_str) for pat in _PATH_PATTERNS):
                issues.append(BlueprintReviewIssue(
                    severity="error",
                    field=field_name,
                    message="参数默认值包含绝对路径，请使用相对路径或占位符。",
                ))
            if _ASSET_ID_PATTERN.search(value_str):
                issues.append(BlueprintReviewIssue(
                    severity="error",
                    field=field_name,
                    message="参数默认值包含资产 ID，请移除项目特定引用。",
                ))
            if _SECRET_PATTERN.search(value_str):
                issues.append(BlueprintReviewIssue(
                    severity="error",
                    field=field_name,
                    message="参数默认值包含敏感关键词，请移除或泛化。",
                ))

        # Runtime requirement hints
        rr = blueprint.runtime_requirements
        py_rr = rr.python
        if isinstance(py_rr, BlueprintRuntimeRequirement):
            if not py_rr.packages and not py_rr.env_hint:
                issues.append(BlueprintReviewIssue(
                    severity="warning",
                    field="runtime_requirements.python",
                    message="未声明 Python 包清单；实例化时可能无法解析依赖。",
                ))

        r_rr = rr.r
        if isinstance(r_rr, BlueprintRuntimeRequirement):
            if not r_rr.packages and not r_rr.env_hint:
                issues.append(BlueprintReviewIssue(
                    severity="warning",
                    field="runtime_requirements.r",
                    message="未声明 R 包清单；实例化时可能无法解析依赖。",
                ))

        if any(issue.severity == "error" for issue in issues):
            return BlueprintReviewResult(
                verdict="fail",
                summary="检测到项目特定信息泄漏，请修正后再审。",
                issues=issues,
            )
        if any(issue.severity == "warning" for issue in issues):
            return BlueprintReviewResult(
                verdict="warn",
                summary="检测到可优化项，请确认后发布。",
                issues=issues,
            )

        return BlueprintReviewResult(
            verdict="pass",
            summary="规则审查通过，未检测到项目特定泄漏。",
            issues=issues,
        )
