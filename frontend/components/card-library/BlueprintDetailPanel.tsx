"use client";

import { Wrench, Radio, Globe, AlertCircle, Calendar, Tag, Layers, Database, Lightbulb } from "lucide-react";

import { CardBlueprint, CardBlueprintIndexEntry, CardBlueprintDraftIndexEntry, BlueprintReviewResult } from "@/lib/types";
import { formatDate } from "./BlueprintCard";

// ---------------------------------------------------------------------------
// Detail Panel (rendered inside the expanded playing card)
// ---------------------------------------------------------------------------

export interface BlueprintDetailPanelProps {
  blueprint: CardBlueprint | null;
  entry?: CardBlueprintIndexEntry | CardBlueprintDraftIndexEntry;
  review?: BlueprintReviewResult | null;
  actions?: React.ReactNode;
  className?: string;
}

function Section({ title, children, icon }: { title: string; children: React.ReactNode; icon?: React.ReactNode }) {
  return (
    <div className="playing-card-detail-section">
      <div className="playing-card-detail-section-title">
        {icon ? <span className="section-icon">{icon}</span> : null}
        {title}
      </div>
      {children}
    </div>
  );
}

export function BlueprintDetailPanel({ blueprint, entry, review, actions, className }: BlueprintDetailPanelProps) {
  if (!blueprint) {
    return (
      <div className={`playing-card-detail empty ${className ?? ""}`.trim()}>
        <p>选择一张分析卡查看详情</p>
      </div>
    );
  }

  const runtimePython = typeof blueprint.runtime_requirements.python === "object"
    ? blueprint.runtime_requirements.python.packages
    : [];
  const runtimeR = typeof blueprint.runtime_requirements.r === "object"
    ? blueprint.runtime_requirements.r.packages
    : [];

  const referenceAssets = blueprint.reference_assets ?? [];
  const useCases = blueprint.use_cases ?? [];

  const useCount = "use_count" in (entry ?? {}) ? (entry as CardBlueprintIndexEntry).use_count : 0;

  return (
    <div className={`playing-card-detail ${className ?? ""}`.trim()}>
      {/* Header */}
      <div className="playing-card-detail-header">
        <div className="playing-card-detail-title">
          <h2>{blueprint.title}</h2>
          <p>{blueprint.summary}</p>
        </div>
        {actions ? <div className="playing-card-detail-actions">{actions}</div> : null}
      </div>

      {/* Meta chips */}
      <div className="playing-card-detail-meta">
        {blueprint.domain && (
          <span className="pill domain-pill">
            <Globe size={10} /> {blueprint.domain}
          </span>
        )}
        {blueprint.tags.map((tag) => (
          <span key={tag} className="pill">{tag}</span>
        ))}
      </div>

      {/* Capabilities */}
      {(blueprint.skills.length > 0 || blueprint.mcp_servers.length > 0 || runtimePython.length > 0 || runtimeR.length > 0 || referenceAssets.length > 0) && (
        <Section title="能力 & 依赖" icon={<Layers size={11} />}>
          <div className="playing-card-detail-chips">
            {blueprint.skills.map((s) => (
              <span key={s} className="capability-chip skill">
                <Wrench size={10} /> {s}
              </span>
            ))}
            {blueprint.mcp_servers.map((s) => (
              <span key={s} className="capability-chip mcp">
                <Radio size={10} /> {s}
              </span>
            ))}
            {runtimePython.length > 0 && (
              <span className="capability-chip runtime" title={runtimePython.join(", ")}>
                Py · {runtimePython.length}
              </span>
            )}
            {runtimeR.length > 0 && (
              <span className="capability-chip runtime" title={runtimeR.join(", ")}>
                R · {runtimeR.length}
              </span>
            )}
            {referenceAssets.map((ref) => (
              <span
                key={ref.ref_id}
                className="capability-chip runtime"
                title={ref.description ?? ref.ref_id}
              >
                <Database size={10} /> {ref.role}{ref.required ? " *" : ""}
              </span>
            ))}
          </div>
        </Section>
      )}

      {/* Use cases (authored by the generalization agent; drives manager retrieval) */}
      {useCases.length > 0 && (
        <Section title="使用场景" icon={<Lightbulb size={11} />}>
          <ul className="playing-card-detail-instructions">
            {useCases.map((useCase, i) => (
              <li key={i}>{useCase}</li>
            ))}
          </ul>
        </Section>
      )}

      {/* Interface (inputs / outputs / parameters) */}
      {(blueprint.inputs_schema.length > 0 || blueprint.outputs_schema.length > 0 || blueprint.parameters.length > 0) && (
        <Section title="接口" icon={<Tag size={11} />}>
          <div className="playing-card-detail-interface">
            {blueprint.inputs_schema.length > 0 && (
              <div className="interface-block">
                <span className="interface-label">输入</span>
                <div className="interface-list">
                  {blueprint.inputs_schema.map((inp) => (
                    <div key={inp.slot} className="interface-row">
                      <span className="interface-name">{inp.label}{inp.required ? " *" : ""}</span>
                      <span className="interface-formats">{inp.accepted_formats.join(", ") || "任意"}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {blueprint.outputs_schema.length > 0 && (
              <div className="interface-block">
                <span className="interface-label">输出</span>
                <div className="interface-list">
                  {blueprint.outputs_schema.map((out) => (
                    <div key={out.role} className="interface-row">
                      <span className="interface-name">{out.label}</span>
                      <span className="interface-formats">{out.artifact_class} · {out.accepted_formats.join(", ")}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {blueprint.parameters.length > 0 && (
              <div className="interface-block">
                <span className="interface-label">参数</span>
                <div className="interface-list">
                  {blueprint.parameters.map((p) => (
                    <div key={p.name} className="interface-row">
                      <span className="interface-name">{p.name}{p.required ? " *" : ""}</span>
                      <span className="interface-formats">{p.type}{p.default != null ? ` · ${String(p.default)}` : ""}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Section>
      )}

      {/* Instructions */}
      {blueprint.instruction_blocks.length > 0 && (
        <Section title="执行指令" icon={<Wrench size={11} />}>
          <ol className="playing-card-detail-instructions">
            {blueprint.instruction_blocks.map((block, i) => (
              <li key={i}>{block}</li>
            ))}
          </ol>
        </Section>
      )}

      {/* Review issues */}
      {review && review.issues.length > 0 && (
        <Section title="审查结果" icon={<AlertCircle size={11} />}>
          <p className="review-summary">{review.summary}</p>
          <div className="review-issues">
            {review.issues.map((issue, i) => (
              <div key={i} className={`review-issue ${issue.severity}`}>
                <div className="review-issue-header">
                  <AlertCircle size={12} />
                  <strong>{issue.severity}</strong>
                  <span>{issue.field}</span>
                </div>
                <div className="review-issue-message">{issue.message}</div>
                {issue.suggested_value ? (
                  <div className="review-issue-suggestion">建议: {issue.suggested_value}</div>
                ) : null}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Provenance footer */}
      <div className="playing-card-detail-footer">
        <span><Calendar size={11} /> {formatDate(blueprint.provenance.created_at) || "未知"}</span>
        {useCount > 0 && <span><Tag size={11} /> 使用 {useCount} 次</span>}
        {blueprint.provenance.last_used_at && (
          <span>最近 {formatDate(blueprint.provenance.last_used_at)}</span>
        )}
      </div>
    </div>
  );
}
