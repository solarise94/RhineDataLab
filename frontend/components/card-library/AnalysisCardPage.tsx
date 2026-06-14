"use client";

import { useState } from "react";

import { ProjectDeckPanel } from "./ProjectDeckPanel";
import { CardLibraryPage } from "./CardLibraryPage";
import { ReferenceDataPanel } from "./ReferenceDataPanel";

type Tab = "drafts" | "global" | "reference";

const TABS: { key: Tab; label: string }[] = [
  { key: "drafts", label: "我的草稿" },
  { key: "global", label: "全局分析卡库" },
  { key: "reference", label: "参考数据" },
];

/**
 * Unified "分析卡" page: one entry that hosts the project draft deck, the
 * global published-card library, and the shared reference-data registry behind
 * a single tab strip. Each tab delegates to its existing panel.
 */
export function AnalysisCardPage({ projectId }: { projectId: string }) {
  const [tab, setTab] = useState<Tab>("drafts");

  return (
    <div>
      <div
        style={{
          display: "flex",
          gap: 4,
          padding: "0 16px",
          borderBottom: "1px solid var(--line)",
          marginBottom: 4,
        }}
      >
        {TABS.map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              style={{
                appearance: "none",
                background: "none",
                border: "none",
                borderBottom: active ? "2px solid var(--blue, #3b82f6)" : "2px solid transparent",
                color: active ? "var(--text)" : "var(--muted)",
                fontWeight: active ? 600 : 500,
                fontSize: 13,
                padding: "10px 14px",
                cursor: "pointer",
              }}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {tab === "drafts" && <ProjectDeckPanel projectId={projectId} />}
      {tab === "global" && <CardLibraryPage embedded />}
      {tab === "reference" && <ReferenceDataPanel />}
    </div>
  );
}
