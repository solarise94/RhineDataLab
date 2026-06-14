"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { SkillHubPanel } from "./SkillHubPanel";
import { McpHubPanel } from "./McpHubPanel";
import { CapabilityInstallPanel } from "./CapabilityInstallPanel";
import { BlueprintDeckPanel } from "@/components/card-library/BlueprintDeckPanel";
type Tab = "skills" | "mcp" | "install" | "deck";

const TABS: { key: Tab; label: string }[] = [
  { key: "skills", label: "Skills" },
  { key: "mcp", label: "MCP" },
  { key: "install", label: "Install" },
  { key: "deck", label: "牌库" },
];

interface CapabilitiesPanelProps {
  projectId: string;
}

const VALID_TABS = new Set<Tab>(["skills", "mcp", "install", "deck"]);

export function CapabilitiesPanel({ projectId }: CapabilitiesPanelProps) {
  const searchParams = useSearchParams();
  const [activeTab, setActiveTab] = useState<Tab>("skills");
  const [focusSkillId, setFocusSkillId] = useState<string | null>(null);
  const [focusMcpId, setFocusMcpId] = useState<string | null>(null);

  useEffect(() => {
    const tab = searchParams.get("tab");
    if (tab && VALID_TABS.has(tab as Tab)) {
      setActiveTab(tab as Tab);
    }
  }, [searchParams]);

  function handleInstalled(kind: "skill" | "mcp", installedId: string) {
    if (kind === "skill") {
      setFocusSkillId(installedId);
      setActiveTab("skills");
    } else {
      setFocusMcpId(installedId);
      setActiveTab("mcp");
    }
  }

  return (
    <div className="stack">
      <div className="capabilities-tabs">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            className={`capabilities-tab ${activeTab === tab.key ? "active" : ""}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "skills" && <SkillHubPanel projectId={projectId} focusId={focusSkillId} />}
      {activeTab === "mcp" && <McpHubPanel projectId={projectId} focusId={focusMcpId} />}
      {activeTab === "install" && (
        <CapabilityInstallPanel projectId={projectId} onInstalled={handleInstalled} />
      )}
      {activeTab === "deck" && <BlueprintDeckPanel />}
    </div>
  );
}
