"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Search, X, Layers, Loader2, Trash2, CheckCircle2, AlertCircle, Pencil, Save } from "lucide-react";

import {
  useProjectCardLibrary,
  useProjectCardDraft,
  useReviewProjectCardDraft,
  usePublishProjectCardDraft,
  useDeleteProjectCardDraft,
  useUpdateProjectCardDraft,
  useReferenceData,
} from "@/lib/hooks";
import { DraftStatus, CardBlueprintDraftIndexEntry, ReferenceAssetRef, UpdateProjectDraftRequest } from "@/lib/types";
import { BlueprintCard } from "./BlueprintCard";
import { BlueprintDetailPanel } from "./BlueprintDetailPanel";
import { useCardExpansion, useIsomorphicLayoutEffect } from "./useCardExpansion";

const STATUS_OPTIONS: { value: DraftStatus | ""; label: string }[] = [
  { value: "", label: "所有状态" },
  { value: "draft", label: "草稿" },
  { value: "needs_review", label: "待审查" },
  { value: "approved", label: "已通过" },
  { value: "rejected", label: "已驳回" },
  { value: "published", label: "已发布" },
];

export function ProjectDeckPanel({ projectId }: { projectId: string }) {
  const searchParams = useSearchParams();
  const { data, isLoading, isError } = useProjectCardLibrary(projectId);
  const referenceDataQuery = useReferenceData();
  const referenceEntries = referenceDataQuery.data?.entries ?? [];
  const [searchQuery, setSearchQuery] = useState("");
  const [domainFilter, setDomainFilter] = useState("");
  const [runtimeFilter, setRuntimeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<DraftStatus | "">("");
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null);
  const { cardRefs, snapshot, flip } = useCardExpansion<HTMLDivElement>();
  const [isEditing, setIsEditing] = useState(false);
  const [editForm, setEditForm] = useState<UpdateProjectDraftRequest>({});
  const [toast, setToast] = useState<{ message: string; kind: "success" | "error" } | null>(null);

  const entries = data?.entries ?? [];

  useEffect(() => {
    const draftIdFromUrl = searchParams.get("draft");
    if (draftIdFromUrl && entries.some((e) => e.draft_id === draftIdFromUrl)) {
      setSelectedDraftId(draftIdFromUrl);
    }
  }, [searchParams, entries]);

  const selectedEntry = useMemo(
    () => entries.find((e) => e.draft_id === selectedDraftId) ?? null,
    [entries, selectedDraftId],
  );

  const allDomains = useMemo(() => {
    const set = new Set<string>();
    for (const e of entries) if (e.domain) set.add(e.domain);
    return [...set].sort();
  }, [entries]);

  const allRuntimeHints = useMemo(() => {
    const set = new Set<string>();
    for (const e of entries) for (const h of e.runtime_hints) if (h) set.add(h);
    return [...set].sort();
  }, [entries]);

  const filtered = useMemo(() => {
    let result = entries;
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter(
        (e) =>
          e.title.toLowerCase().includes(q) ||
          e.summary.toLowerCase().includes(q) ||
          e.tags.some((t) => t.toLowerCase().includes(q)) ||
          e.domain.toLowerCase().includes(q),
      );
    }
    if (domainFilter) {
      result = result.filter((e) => e.domain === domainFilter);
    }
    if (runtimeFilter) {
      result = result.filter((e) => e.runtime_hints.includes(runtimeFilter));
    }
    if (statusFilter) {
      result = result.filter((e) => e.status === statusFilter);
    }
    return result;
  }, [entries, searchQuery, domainFilter, runtimeFilter, statusFilter]);

  const hasFilters = domainFilter || runtimeFilter || statusFilter;

  const draftQuery = useProjectCardDraft(projectId, selectedDraftId);
  const reviewMutation = useReviewProjectCardDraft(projectId);
  const publishMutation = usePublishProjectCardDraft(projectId);
  const deleteMutation = useDeleteProjectCardDraft(projectId);
  const updateMutation = useUpdateProjectCardDraft(projectId);

  useEffect(() => {
    if (!isEditing) return;
    const bp = draftQuery.data?.draft.blueprint;
    if (!bp) return;
    const py = typeof bp.runtime_requirements.python === "object" ? bp.runtime_requirements.python.packages : [];
    const r = typeof bp.runtime_requirements.r === "object" ? bp.runtime_requirements.r.packages : [];
    setEditForm({
      title: bp.title,
      summary: bp.summary,
      tags: bp.tags,
      domain: bp.domain,
      instruction_blocks: bp.instruction_blocks,
      python_packages: py,
      r_packages: r,
      use_cases: bp.use_cases,
      reference_assets: bp.reference_assets,
    });
  }, [isEditing, draftQuery.data?.draft.blueprint]);

  function showToast(message: string, kind: "success" | "error" = "success") {
    setToast({ message, kind });
    setTimeout(() => setToast(null), 3000);
  }

  function collapse() {
    snapshot();
    setIsEditing(false);
    setSelectedDraftId(null);
  }

  // Clicking empty space in the content area (not on a card) collapses the
  // open card.
  function handleContentClick(e: React.MouseEvent) {
    if (!selectedDraftId) return;
    if ((e.target as HTMLElement).closest(".bp-card")) return;
    collapse();
  }

  function handleSelect(draftId: string) {
    // Snapshot BEFORE the state change so FLIP can animate the reflow.
    snapshot();
    setIsEditing(false);
    setSelectedDraftId((cur) => (cur === draftId ? null : draftId));
  }

  // Animate every card the reflow displaced.
  useIsomorphicLayoutEffect(() => {
    flip();
  }, [selectedDraftId, flip]);

  // Escape collapses the open card.
  useEffect(() => {
    if (!selectedDraftId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") collapse();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDraftId]);

  function handleEditStart() {
    setIsEditing(true);
  }

  function handleEditCancel() {
    setIsEditing(false);
  }

  function handleEditSave() {
    if (!selectedDraftId) return;
    const payload: UpdateProjectDraftRequest = {
      ...editForm,
      tags: editForm.tags?.map((t) => t.trim()).filter(Boolean),
      python_packages: editForm.python_packages?.map((p) => p.trim()).filter(Boolean),
      r_packages: editForm.r_packages?.map((p) => p.trim()).filter(Boolean),
      use_cases: editForm.use_cases?.map((t) => t.trim()).filter(Boolean),
      reference_assets: (editForm.reference_assets ?? [])
        .filter((r) => r.ref_id && r.role.trim())
        .map((r) => ({ ref_id: r.ref_id, role: r.role.trim(), required: r.required, description: r.description?.trim() || null })),
    };
    updateMutation.mutate(
      { draftId: selectedDraftId, payload },
      {
        onSuccess: () => {
          setIsEditing(false);
          showToast("已修正并重置为草稿状态", "success");
        },
        onError: () => showToast("修正失败", "error"),
      },
    );
  }

  function handleReview() {
    if (!selectedDraftId) return;
    reviewMutation.mutate(selectedDraftId, {
      onSuccess: (result) => {
        const label = result.review.verdict === "fail" ? "审查完成：未通过" : "审查完成";
        showToast(label, result.review.verdict === "fail" ? "error" : "success");
      },
      onError: () => showToast("审查失败", "error"),
    });
  }

  function handlePublish() {
    if (!selectedDraftId) return;
    publishMutation.mutate(selectedDraftId, {
      onSuccess: () => {
        showToast("已发布到全局分析卡库", "success");
        collapse();
      },
      onError: () => showToast("发布失败", "error"),
    });
  }

  function handleDelete() {
    if (!selectedDraftId) return;
    deleteMutation.mutate(selectedDraftId, {
      onSuccess: () => {
        collapse();
        showToast("已删除", "success");
      },
      onError: () => showToast("删除失败", "error"),
    });
  }

  const anyLoading = reviewMutation.isPending || publishMutation.isPending || deleteMutation.isPending || updateMutation.isPending;

  const expandedContent = selectedEntry ? (
    isEditing ? (
      <>
        <div className="bp-card-toolbar">
          <button
            type="button"
            className="btn secondary"
            onClick={handleEditCancel}
            disabled={updateMutation.isPending}
          >
            <X size={14} /> 取消
          </button>
          <button
            type="button"
            className="btn primary"
            onClick={handleEditSave}
            disabled={updateMutation.isPending}
          >
            {updateMutation.isPending ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
            保存
          </button>
        </div>
        <div style={{ display: "grid", gap: 12, padding: "16px 18px" }}>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>标题</span>
            <input
              type="text"
              value={editForm.title ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, title: e.target.value }))}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>摘要</span>
            <textarea
              value={editForm.summary ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, summary: e.target.value }))}
              rows={3}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)", resize: "vertical" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>领域</span>
            <input
              type="text"
              value={editForm.domain ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, domain: e.target.value }))}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>标签（逗号分隔）</span>
            <input
              type="text"
              value={editForm.tags?.join(", ") ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, tags: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) }))}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>指令（每行一条）</span>
            <textarea
              value={editForm.instruction_blocks?.join("\n") ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, instruction_blocks: e.target.value.split("\n").map((t) => t.trim()).filter(Boolean) }))}
              rows={4}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)", resize: "vertical" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>Python 包（逗号分隔）</span>
            <input
              type="text"
              value={editForm.python_packages?.join(", ") ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, python_packages: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) }))}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>R 包（逗号分隔）</span>
            <input
              type="text"
              value={editForm.r_packages?.join(", ") ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, r_packages: e.target.value.split(",").map((t) => t.trim()).filter(Boolean) }))}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>使用场景（每行一条，供 manager 检索复用）</span>
            <textarea
              value={editForm.use_cases?.join("\n") ?? ""}
              onChange={(e) => setEditForm((prev) => ({ ...prev, use_cases: e.target.value.split("\n").map((t) => t.trim()).filter(Boolean) }))}
              rows={3}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)", resize: "vertical" }}
            />
          </label>
          <div style={{ display: "grid", gap: 6, fontSize: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontWeight: 600 }}>参考数据依赖（环境级，如 GTF）</span>
              <button
                type="button"
                className="btn secondary"
                style={{ fontSize: 11, padding: "2px 8px" }}
                onClick={() => setEditForm((prev) => ({
                  ...prev,
                  reference_assets: [...(prev.reference_assets ?? []), { ref_id: "", role: "", required: true, description: null } as ReferenceAssetRef],
                }))}
              >
                + 添加
              </button>
            </div>
            {(editForm.reference_assets ?? []).map((ref, idx) => (
              <div key={idx} style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr auto auto", gap: 6, alignItems: "center" }}>
                <select
                  value={ref.ref_id}
                  onChange={(e) => setEditForm((prev) => {
                    const arr = [...(prev.reference_assets ?? [])];
                    arr[idx] = { ...arr[idx], ref_id: e.target.value };
                    return { ...prev, reference_assets: arr };
                  })}
                  style={{ padding: "6px 8px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
                >
                  <option value="">选择参考数据…</option>
                  {referenceEntries.map((rd) => (
                    <option key={rd.ref_id} value={rd.ref_id}>{rd.name} · {rd.kind}</option>
                  ))}
                </select>
                <input
                  placeholder="role，如 gene_annotation"
                  value={ref.role}
                  onChange={(e) => setEditForm((prev) => {
                    const arr = [...(prev.reference_assets ?? [])];
                    arr[idx] = { ...arr[idx], role: e.target.value };
                    return { ...prev, reference_assets: arr };
                  })}
                  style={{ padding: "6px 8px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
                />
                <label style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 3, whiteSpace: "nowrap" }}>
                  <input
                    type="checkbox"
                    checked={ref.required}
                    onChange={(e) => setEditForm((prev) => {
                      const arr = [...(prev.reference_assets ?? [])];
                      arr[idx] = { ...arr[idx], required: e.target.checked };
                      return { ...prev, reference_assets: arr };
                    })}
                  />
                  必需
                </label>
                <button
                  type="button"
                  onClick={() => setEditForm((prev) => ({ ...prev, reference_assets: (prev.reference_assets ?? []).filter((_, i) => i !== idx) }))}
                  style={{ background: "none", border: "none", cursor: "pointer", color: "var(--red)", display: "flex", alignItems: "center" }}
                  title="移除"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
            {referenceEntries.length === 0 && (
              <span style={{ color: "var(--muted)" }}>参考数据注册表为空，先到"分析卡 → 参考数据"上传。</span>
            )}
          </div>
        </div>
      </>
    ) : (
      <>
        <div className="bp-card-toolbar">
          <button
            type="button"
            className="btn secondary"
            onClick={handleEditStart}
            disabled={anyLoading || selectedEntry.status === "published"}
          >
            <Pencil size={14} /> 修正
          </button>
          <button
            type="button"
            className="btn secondary"
            onClick={handleReview}
            disabled={anyLoading || selectedEntry.status === "published"}
          >
            {reviewMutation.isPending ? <Loader2 size={14} className="spin" /> : null}
            审查
          </button>
          <button
            type="button"
            className="btn primary"
            onClick={handlePublish}
            disabled={anyLoading || selectedEntry.status !== "approved"}
          >
            {publishMutation.isPending ? <Loader2 size={14} className="spin" /> : null}
            发布到全局分析卡库
          </button>
          <button
            type="button"
            className="btn secondary"
            style={{ color: "var(--red)" }}
            onClick={handleDelete}
            disabled={anyLoading || selectedEntry.status === "published"}
          >
            {deleteMutation.isPending ? <Loader2 size={14} className="spin" /> : <Trash2 size={14} />}
            删除
          </button>
        </div>
        <BlueprintDetailPanel
          blueprint={draftQuery.data?.draft.blueprint ?? null}
          entry={selectedEntry}
          review={draftQuery.data?.draft.review ?? null}
        />
      </>
    )
  ) : null;

  return (
    <div className="card-library-page">
      <div className="card-library-header">
        <div>
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>项目分析卡（草稿）</h2>
          <p style={{ margin: 0, color: "var(--muted)", fontSize: 12 }}>把项目 card 泛化审查后发布到全局分析卡库</p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <div className="search-input-wrap">
            <Search size={14} style={{ color: "var(--muted)", flexShrink: 0 }} />
            <input
              type="text"
              placeholder="搜索草稿…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
            {searchQuery ? (
              <button type="button" onClick={() => setSearchQuery("")} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--muted)" }}>
                <X size={14} />
              </button>
            ) : null}
          </div>
          {allDomains.length > 0 && (
            <select
              value={domainFilter}
              onChange={(e) => setDomainFilter(e.target.value)}
              style={{ padding: "6px 8px", border: "1px solid var(--line)", borderRadius: 6, background: "var(--bg)", fontSize: 13, color: "var(--text)" }}
            >
              <option value="">所有领域</option>
              {allDomains.map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          )}
          {allRuntimeHints.length > 0 && (
            <select
              value={runtimeFilter}
              onChange={(e) => setRuntimeFilter(e.target.value)}
              style={{ padding: "6px 8px", border: "1px solid var(--line)", borderRadius: 6, background: "var(--bg)", fontSize: 13, color: "var(--text)" }}
            >
              <option value="">所有 Runtime</option>
              {allRuntimeHints.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          )}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as DraftStatus | "")}
            style={{ padding: "6px 8px", border: "1px solid var(--line)", borderRadius: 6, background: "var(--bg)", fontSize: 13, color: "var(--text)" }}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          {hasFilters && (
            <button
              type="button"
              className="btn secondary"
              style={{ fontSize: 12, padding: "4px 8px" }}
              onClick={() => {
                setDomainFilter("");
                setRuntimeFilter("");
                setStatusFilter("");
              }}
            >
              <X size={12} /> 清除筛选
            </button>
          )}
        </div>
      </div>

      {toast && (
        <div
          style={{
            margin: "0 16px",
            padding: "8px 12px",
            borderRadius: 6,
            fontSize: 13,
            fontWeight: 500,
            background: toast.kind === "error" ? "var(--red-bg)" : "var(--green-bg)",
            color: toast.kind === "error" ? "var(--red-dark)" : "var(--green-dark)",
          }}
        >
          {toast.kind === "error" ? <AlertCircle size={14} style={{ verticalAlign: -2 }} /> : <CheckCircle2 size={14} style={{ verticalAlign: -2 }} />}
          {" "}{toast.message}
        </div>
      )}

      <div className="card-library-content" onClick={handleContentClick}>
        {isLoading && <div className="empty-state">加载草稿…</div>}
        {isError && <div className="empty-state" style={{ color: "var(--red)" }}>草稿加载失败</div>}
        {!isLoading && !isError && filtered.length === 0 && (
          <div className="empty-state">
            <Layers size={32} style={{ color: "var(--muted)", marginBottom: 8 }} />
            <p>{entries.length === 0 ? "草稿为空。从卡片详情或模块卡片把 card 加入草稿。" : "没有匹配的卡"}</p>
          </div>
        )}
        {!isLoading && !isError && filtered.length > 0 && (
          <div className="card-library-grid">
            {filtered.map((entry) => {
              const isExpanded = selectedDraftId === entry.draft_id;
              return (
                <BlueprintCard
                  key={entry.draft_id}
                  ref={(el) => { cardRefs.current[entry.draft_id] = el; }}
                  entry={entry}
                  isExpanded={isExpanded}
                  onSelect={() => handleSelect(entry.draft_id)}
                  status={entry.status}
                  expandedChildren={isExpanded ? expandedContent : undefined}
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
