"use client";

import { useState, useMemo, useEffect, useRef } from "react";
import { useProjectSkillLibrary } from "@/lib/hooks";

interface SkillHubPanelProps {
  projectId: string;
  focusId?: string | null;
}

export function SkillHubPanel({ projectId, focusId }: SkillHubPanelProps) {
  const sectionRef = useRef<HTMLElement>(null);
  const libraryQuery = useProjectSkillLibrary(projectId);
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    if (focusId) setSelectedId(focusId);
  }, [focusId]);

  const items = (libraryQuery.data?.items as Array<Record<string, unknown>> | undefined) ?? [];

  useEffect(() => {
    if (focusId && items.some((item) => item.id === focusId)) {
      sectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [focusId, items]);

  const filtered = useMemo(() => {
    if (!search.trim()) return items;
    const q = search.toLowerCase();
    return items.filter(
      (item) =>
        String(item.name ?? "").toLowerCase().includes(q) ||
        String(item.id ?? "").toLowerCase().includes(q) ||
        String(item.summary_short ?? item.summary ?? "").toLowerCase().includes(q),
    );
  }, [items, search]);

  const selected = items.find((item) => item.id === selectedId) ?? items[0] ?? null;

  return (
    <section ref={sectionRef} className="panel">
      <div className="panel-header">
        <h3>Skill 库</h3>
        <span>
          {filtered.length} 个{search.trim() ? "匹配" : ""}
        </span>
      </div>
      <div className="panel-body stack">
        <label className="settings-field">
          <span>搜索</span>
          <input
            type="text"
            placeholder="按名称、ID 或摘要筛选…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <div className="settings-library-grid">
          <div className="settings-library-list">
            {libraryQuery.isLoading ? (
              <div className="settings-empty">正在加载 Skill 库…</div>
            ) : null}
            {!libraryQuery.isLoading && !filtered.length ? (
              <div className="settings-empty">没有匹配的 Skill</div>
            ) : null}
            {filtered.map((item) => (
              <button
                key={String(item.id)}
                type="button"
                className={`settings-library-item ${selected && String(selected.id) === String(item.id) ? "active" : ""}`}
                onClick={() => setSelectedId(String(item.id))}
              >
                <strong>{String(item.name ?? item.id)}</strong>
                <span>{String(item.id)}</span>
                {item.enabled === false ? <em>已禁用</em> : null}
              </button>
            ))}
          </div>
          <div className="settings-library-detail">
            {selected ? (
              <div className="settings-detail-header">
                <div>
                  <h4>{String(selected.name ?? selected.id)}</h4>
                  <p>{String(selected.summary_long ?? selected.summary ?? "")}</p>
                </div>
              </div>
            ) : null}
            {selected ? (
              <div className="settings-kv-list">
                <div>
                  <strong>ID</strong>
                  <span>{String(selected.id)}</span>
                </div>
                <div>
                  <strong>使用场景</strong>
                  <span>
                    {Array.isArray(selected.use_cases)
                      ? (selected.use_cases as string[]).join(", ") || "—"
                      : "—"}
                  </span>
                </div>
                <div>
                  <strong>运行时要求</strong>
                  <span>
                    {Array.isArray(selected.runtime_requirements)
                      ? (selected.runtime_requirements as string[]).join(", ") || "—"
                      : "—"}
                  </span>
                </div>
                <div>
                  <strong>支持运行时</strong>
                  <span>
                    {Array.isArray(selected.supported_runtimes)
                      ? (selected.supported_runtimes as string[]).join(", ") || "—"
                      : "—"}
                  </span>
                </div>
                <div>
                  <strong>兼容性提示</strong>
                  <span>
                    {Array.isArray(selected.compatibility_notes)
                      ? (selected.compatibility_notes as string[]).join(", ") || "—"
                      : "—"}
                  </span>
                </div>
                <div>
                  <strong>启动提示</strong>
                  <span>{String(selected.launch_hint ?? "—")}</span>
                </div>
                <div>
                  <strong>来源</strong>
                  <span>{String(selected.source_kind ?? "—")}</span>
                </div>
              </div>
            ) : (
              <div className="settings-empty">在左侧列表中选择一个 Skill 查看详情</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
