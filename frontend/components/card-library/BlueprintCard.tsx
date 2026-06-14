"use client";

import { forwardRef } from "react";
import { Globe, Wrench, Radio, Layers } from "lucide-react";

import { CardBlueprintIndexEntry, CardBlueprintDraftIndexEntry, DraftStatus } from "@/lib/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function formatDate(value: string | null | undefined) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", year: "numeric" });
}

const STATUS_LABEL: Record<DraftStatus, string> = {
  draft: "草",
  needs_review: "审",
  approved: "过",
  rejected: "驳",
  published: "发",
};

const STATUS_STYLE: Record<DraftStatus, { bg: string; color: string }> = {
  draft: { bg: "var(--muted-bg, #f3f4f6)", color: "var(--text-secondary, #6b7280)" },
  needs_review: { bg: "var(--amber-bg, #fef3c7)", color: "var(--amber-dark, #92400e)" },
  approved: { bg: "var(--green-bg, #d1fae5)", color: "var(--green-dark, #065f46)" },
  rejected: { bg: "var(--red-bg, #fee2e2)", color: "var(--red-dark, #991b1b)" },
  published: { bg: "var(--blue-bg, #dbeafe)", color: "var(--blue-dark, #1e40af)" },
};

const DOMAIN_ACCENT: Record<string, { icon: React.ReactNode; hue: string }> = {
  bioinformatics: { icon: <Globe size={13} />, hue: "#22c55e" },
  genomics: { icon: <Radio size={13} />, hue: "#8b5cf6" },
  statistics: { icon: <Wrench size={13} />, hue: "#3b82f6" },
  visualization: { icon: <Layers size={13} />, hue: "#f59e0b" },
};

function domainAccent(domain: string) {
  return DOMAIN_ACCENT[domain.toLowerCase()] ?? { icon: <Layers size={13} />, hue: "var(--blue)" };
}

// ---------------------------------------------------------------------------
// Blueprint Card — clean modern card.
//
// The grid item is a <div> (.bp-card) so that, when expanded, it can host
// interactive controls (delete / publish / edit buttons, form inputs) inside
// .bp-card-expanded. The clickable face is a real <button> (.bp-card-face);
// the FLIP ref is attached to the outer div by the parent grid.
// ---------------------------------------------------------------------------

export interface BlueprintCardProps {
  entry: CardBlueprintIndexEntry | CardBlueprintDraftIndexEntry;
  isExpanded?: boolean;
  onSelect: () => void;
  status?: DraftStatus | null;
  /** Rendered in-flow below the face when expanded (actions + detail). */
  expandedChildren?: React.ReactNode;
}

export const BlueprintCard = forwardRef<HTMLDivElement, BlueprintCardProps>(
  function BlueprintCard({ entry, isExpanded = false, onSelect, status, expandedChildren }, ref) {
    const indexEntry = entry as CardBlueprintIndexEntry;
    const isDraft = "draft_id" in entry;
    const statusStyle = status ? STATUS_STYLE[status] : null;
    const accent = domainAccent(entry.domain);
    const useCount = "use_count" in entry ? indexEntry.use_count : 0;

    return (
      <div
        ref={ref}
        className={`bp-card ${isExpanded ? "expanded" : ""}`}
        style={{ "--accent": accent.hue } as React.CSSProperties}
      >
        <button
          type="button"
          className="bp-card-face"
          onClick={onSelect}
          aria-expanded={isExpanded}
          title={isExpanded ? "收起" : "展开详情"}
        >
          <div className="bp-card-badge-header">
            <span className="bp-card-clip" aria-hidden />
            <div className="bp-card-identity">
              <span className="bp-card-avatar">{accent.icon}</span>
              <div className="bp-card-copy">
                <div className="bp-card-title" title={entry.title}>{entry.title}</div>
                <div className="bp-card-status-row">
                  <span className="bp-card-domain-pill" title={entry.domain}>
                    {entry.domain || "通用"}
                  </span>
                  {status && statusStyle ? (
                    <span
                      className="bp-card-badge"
                      style={{ background: statusStyle.bg, color: statusStyle.color }}
                      title={status}
                    >
                      {STATUS_LABEL[status]}
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
          </div>

          <div className="bp-card-body">
            <p className="bp-card-summary">{entry.summary || "暂无摘要"}</p>
            <div className="bp-card-meta">
              {entry.skills.length > 0 && (
                <span title={`技能: ${entry.skills.join(", ")}`}>
                  <Wrench size={10} /> {entry.skills.length}
                </span>
              )}
              {entry.mcp_servers.length > 0 && (
                <span title={`MCP: ${entry.mcp_servers.join(", ")}`}>
                  <Radio size={10} /> {entry.mcp_servers.length}
                </span>
              )}
              {entry.runtime_hints.length > 0 && (
                <span title={entry.runtime_hints.join(", ")}>{entry.runtime_hints[0]}</span>
              )}
              {isDraft ? (
                <span>DRAFT</span>
              ) : useCount > 0 ? (
                <span>×{useCount}</span>
              ) : null}
            </div>
          </div>
        </button>

        {isExpanded && expandedChildren ? (
          <div className="bp-card-expanded" onClick={(e) => e.stopPropagation()}>
            {expandedChildren}
          </div>
        ) : null}
      </div>
    );
  },
);
