"use client";

import { useMemo, useRef, useEffect, useState } from "react";
import {
  CheckCircle2,
  Files,
  Pencil,
  Play,
  RotateCcw,
  Trash2,
  Sparkles,
  Archive,
  TriangleAlert,
  Bookmark,
  Loader2,
} from "lucide-react";
import { Card, PythonRuntime, RRuntime, WorkerCapability, ExecutorProfile, WorkItem } from "@/lib/types";
import { CardStatusBadge } from "./CardStatusBadge";
import { SpecialistAvatar } from "./SpecialistAvatar";
import { FileBag } from "./FileBag";
import { CardPage, EMPTY_CARD_PAGE_BY_ID, useWorkspaceUiStore } from "@/lib/stores/workspace-ui-store";
import { latestManagerReview } from "@/lib/card-review";
import { useAddCardToProjectLibrary } from "@/lib/hooks";
import { useRouter } from "next/navigation";

function preferredExecutorProfile(profiles: ExecutorProfile[], workerType?: string) {
  if (!workerType) return profiles[0];
  const candidates = workerType ? profiles.filter((profile) => profile.worker_type === workerType) : profiles;
  const preferredAuthMode = workerType === "pi" || workerType === "opencode" ? "project_api" : "cli_native";
  return candidates.find((profile) => profile.auth_mode === preferredAuthMode) ?? candidates[0];
}

export function ModuleCard({
  projectId,
  card,
  active,
  readOnly = false,
  onSelect,
  onStartRun,
  onReviewRun,
  onRerunCard,
  onRunSubgraph,
  onAskManager,
  onPreviewAsset,
  workerCapabilities = [],
  executorProfiles = [],
  selectedWorkerType,
  selectedProfileId,
  onSelectWorker,
  onSelectProfile,
  pythonRuntimes = [],
  rRuntimes = [],
  globalPythonRuntime,
  globalRRuntime,
  selectedPythonRuntime,
  selectedRRuntime,
  onSelectPythonRuntime,
  onSelectRRuntime,
  workItem,
}: {
  projectId: string;
  card: Card;
  active: boolean;
  readOnly?: boolean;
  onSelect: (card: Card) => void;
  onStartRun: (card: Card) => void;
  onReviewRun: (card: Card) => void;
  onRerunCard?: (card: Card, propagate?: string) => void;
  onRunSubgraph?: (card: Card) => void;
  onAskManager?: (text: string) => void;
  onPreviewAsset?: (assetId: string, cardId?: string) => void;
  workerCapabilities?: WorkerCapability[];
  executorProfiles?: ExecutorProfile[];
  selectedWorkerType?: string;
  selectedProfileId?: string;
  onSelectWorker?: (card: Card, workerType: string) => void;
  onSelectProfile?: (card: Card, profileId: string) => void;
  pythonRuntimes?: PythonRuntime[];
  rRuntimes?: RRuntime[];
  globalPythonRuntime?: string;
  globalRRuntime?: string;
  selectedPythonRuntime?: string;
  selectedRRuntime?: string;
  onSelectPythonRuntime?: (card: Card, runtime?: string) => void;
  onSelectRRuntime?: (card: Card, runtime?: string) => void;
  workItem?: WorkItem;
}) {
  const cardPages = useWorkspaceUiStore((s) => s.cardPageByProject[projectId] ?? EMPTY_CARD_PAGE_BY_ID);
  const setCardPage = useWorkspaceUiStore((s) => s.setCardPage);
  const storedPage = cardPages[card.card_id];
  const fileCount = card.outputs.filter((o) => o.asset_id).length;
  const visibleManagerReview = latestManagerReview(card.manager_review);

  const saveMutation = useAddCardToProjectLibrary();
  const router = useRouter();
  const [saveToast, setSaveToast] = useState<string | null>(null);

  const isGhost = card.status === "proposed";
  const isRunning = card.status === "running" || card.status === "reviewing";
  const isDormant = card.status === "cancelled" || card.status === "rejected";
  const configuredWorkers = workerCapabilities.filter((item) => item.configured);
  const enabledProfiles = executorProfiles.filter((p) => p.enabled);
  const fallbackProfile = preferredExecutorProfile(enabledProfiles, selectedWorkerType);
  const effectiveSelectedProfileId =
    selectedProfileId && enabledProfiles.some((p) => p.profile_id === selectedProfileId)
      ? selectedProfileId
      : fallbackProfile?.profile_id;
  const effectiveSelectedProfile = enabledProfiles.find((p) => p.profile_id === effectiveSelectedProfileId);
  const executorCompatibility = effectiveSelectedProfile
    ? executorCompatibilityCopy(effectiveSelectedProfile.worker_type)
    : null;
  const globalRuntimeLabel = globalPythonRuntime && globalPythonRuntime !== "__system__" ? globalPythonRuntime : "系统默认";
  const globalRRuntimeLabel = globalRRuntime && globalRRuntime !== "__system__" ? globalRRuntime : "系统默认";
  const cardCtx = card.executor_context as Record<string, unknown> | null | undefined;
  const cardBindings = cardCtx?.runtime_bindings as { conda_env?: string | null; r_env?: string | null } | undefined;
  const effectivePythonLabel = cardBindings?.conda_env
    ? `卡片固定: ${cardBindings.conda_env}`
    : `项目默认: ${globalRuntimeLabel}`;
  const effectiveRLabel = cardBindings?.r_env
    ? `卡片固定: ${cardBindings.r_env}`
    : `项目默认: ${globalRRuntimeLabel}`;

  const pages: CardPage[] = isDormant
    ? ["specialist", "result", "archive"]
    : ["specialist", "result", "files"];
  const currentPage = pages.includes((storedPage as CardPage | undefined) ?? "specialist")
    ? ((storedPage as CardPage | undefined) ?? (isDormant ? "archive" : "specialist"))
    : (isDormant ? "archive" : "specialist");
  const pageIndex = pages.indexOf(currentPage);

  const slideOffset = useMemo(() => -(pageIndex * 25), [pageIndex]);
  const collapsedSummary = card.progress_note || card.summary || card.why || "等待执行";
  const attentionCount = workItem?.dependency_attention_count ?? 0;
  const attentionSeverity = workItem?.attention_severity ?? "warning";
  const paperSlotRef = useRef<HTMLDivElement>(null);
  const lastWheelSwitchRef = useRef<number>(0);
  const currentPageRef = useRef(currentPage);
  const pagesRef = useRef(pages);
  useEffect(() => { currentPageRef.current = currentPage; }, [currentPage]);
  useEffect(() => { pagesRef.current = pages; }, [pages]);

  useEffect(() => {
    const el = paperSlotRef.current;
    if (!el || !active) return;
    const handler = (e: WheelEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;

      const scroller = target.closest(".page-content-scroll") as HTMLElement | null;
      if (!scroller) return;

      const atTop = scroller.scrollTop <= 0;
      const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1;
      const wantDown = e.deltaY > 0;
      const canScrollInside = (wantDown && !atBottom) || (!wantDown && !atTop);
      if (canScrollInside) return;

      const pagesArr = pagesRef.current;
      const idx = pagesArr.indexOf(currentPageRef.current);
      const nextIdx = wantDown ? idx + 1 : idx - 1;
      if (nextIdx < 0 || nextIdx >= pagesArr.length) return;
      if (performance.now() - lastWheelSwitchRef.current < 350) {
        e.preventDefault();
        return;
      }

      e.preventDefault();
      lastWheelSwitchRef.current = performance.now();
      setCardPage(projectId, card.card_id, pagesArr[nextIdx]);
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, [active, projectId, card.card_id, setCardPage]);

  function handleDot(page: CardPage, e: React.MouseEvent) {
    e.stopPropagation();
    setCardPage(projectId, card.card_id, page);
  }

  function sendToManager(text: string, e: React.MouseEvent) {
    e.stopPropagation();
    onAskManager?.(text);
  }

  function handleSaveToLibrary(e: React.MouseEvent) {
    e.stopPropagation();
    if (saveMutation.isPending) return;
    saveMutation.mutate(
      { projectId, cardId: card.card_id },
      {
        onSuccess: (result) => {
          const msg = result.warnings.length
            ? `已加入分析卡草稿（${result.warnings.length} 条警告）`
            : "已加入分析卡草稿";
          setSaveToast(msg);
          setTimeout(() => setSaveToast(null), 2000);
          router.push(`/projects/${projectId}/card-library?draft=${result.draft_id}`);
        },
        onError: () => {
          setSaveToast("加入分析卡草稿失败");
          setTimeout(() => setSaveToast(null), 3000);
        },
      },
    );
  }

  return (
    <div
      className={`task-specialist-card ${active ? "active" : ""} ${isGhost ? "ghost" : ""} ${isRunning ? "running" : ""} ${isDormant ? "dormant" : ""}`}
      onClick={() => onSelect(card)}
      data-card-id={card.card_id}
    >
      {/* Connection anchors for SVG lines */}
      <div className="card-anchor card-anchor-in" data-anchor={`in-${card.card_id}`} />
      <div className="card-anchor card-anchor-out" data-anchor={`out-${card.card_id}`} />

      {/* ID Badge / File Bag Header */}
      <div className="specialist-badge-header">
        <div className="badge-clip"></div>
        <div className="specialist-badge-identity">
          <SpecialistAvatar name={card.title} status={card.status} />
          <div className="specialist-badge-copy">
            <div className="badge-title" title={card.title}>{card.title}</div>
            <div className="badge-status-row">
              <CardStatusBadge status={card.status} />
              {card.aggregate_status ? <span className="pill badge-pill">{card.aggregate_status}</span> : null}
              {attentionCount > 0 ? (
                <span className={`attention-badge ${attentionSeverity}`} title={`${attentionCount} dependency attention issue(s)`}>
                  <TriangleAlert size={11} /> ATTENTION
                </span>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      <div className="specialist-card-collapsed">
        <div className="specialist-card-collapsed-summary">{collapsedSummary}</div>
        <div className="specialist-card-collapsed-meta">
          <span>{card.inputs.length} in</span>
          <span>{card.outputs.length} out</span>
          <span>{fileCount} files</span>
          {attentionCount > 0 ? <span className={`attention-meta ${attentionSeverity}`}>{attentionCount} attention</span> : null}
        </div>
      </div>

      <div className="file-bag-container">
        <div className="file-bag-tabs" role="tablist" aria-label={`${card.title} card pages`}>
          <button className={`file-bag-tab ${currentPage === "specialist" ? "active" : ""}`} onClick={(e) => handleDot("specialist", e)}>封面</button>
          <button className={`file-bag-tab ${currentPage === "result" ? "active" : ""}`} onClick={(e) => handleDot("result", e)}>结果</button>
          <button className={`file-bag-tab ${(currentPage === "files" || currentPage === "archive") ? "active" : ""}`} onClick={(e) => handleDot(isDormant ? "archive" : "files", e)}>
            {isDormant ? <Archive size={11} /> : <Files size={11} />} {isDormant ? "归档袋" : "文件袋"}
          </button>
        </div>

        <div className="file-bag-paper-slot" ref={paperSlotRef}>
          <div className="file-bag-paper-slider file-bag-paper-slider-4" style={{ transform: `translateY(${slideOffset}%)` }}>
            
            {/* ─── Page 1: Specialist (Cover) ─── */}
            <div className="file-bag-page">
              <div className="page-content-scroll">
                <div className="badge-summary">
                  {card.progress_note || card.summary || "等待执行"}
                </div>
                
                <div className="badge-stats-row">
                  <div className="badge-stat"><span>📥</span> {card.inputs.length} inputs</div>
                  <div className="badge-stat"><span>📤</span> {card.outputs.length} outputs</div>
                </div>

                {saveToast && (
                  <div
                    style={{
                      marginTop: 10,
                      fontSize: 11,
                      fontWeight: 500,
                      color: saveToast.includes("失败") ? "var(--red)" : "var(--green)",
                    }}
                  >
                    {saveToast}
                  </div>
                )}
                <div className="inline-actions" style={{ marginTop: 12 }}>
                  {onAskManager && (
                    <button className="btn secondary" style={{ fontSize: 10, padding: "4px 8px", flex: 1 }} onClick={(e) => sendToManager(`请解释 ${card.title} 的运行情况和当前状态`, e)}>
                      <Sparkles size={12} /> 解释
                    </button>
                  )}
                  {card.status === "accepted" && onAskManager && (
                    <button className="btn secondary" style={{ fontSize: 10, padding: "4px 8px", flex: 1 }} onClick={(e) => sendToManager(`请帮我修改模块 ${card.title}，我想调整分析参数或目标`, e)}>
                      <Pencil size={12} /> 修改
                    </button>
                  )}
                  {isDormant && onAskManager ? (
                    <button className="btn secondary" style={{ fontSize: 10, padding: "4px 8px", flex: 1 }} onClick={(e) => sendToManager(`请恢复卡片 ${card.title}，必要时同步恢复关联模块，并重新纳入蓝图`, e)}>
                      <RotateCcw size={12} /> 恢复
                    </button>
                  ) : null}
                  <button
                    className="btn secondary"
                    style={{ fontSize: 10, padding: "4px 8px", flex: 1 }}
                    onClick={handleSaveToLibrary}
                    disabled={saveMutation.isPending}
                    title="加入分析卡草稿"
                  >
                    {saveMutation.isPending ? <Loader2 size={12} className="spin" /> : <Bookmark size={12} />}
                    加入分析卡草稿
                  </button>
                </div>

                {card.status === "planned" ? (
                  <div className="executor-run-control">
                    <label className="executor-select-label" onClick={(e) => e.stopPropagation()}>
                      <span>执行器</span>
                      <select
                        value={effectiveSelectedProfileId ?? enabledProfiles[0]?.profile_id ?? ""}
                        onChange={(e) => {
                          const profile = enabledProfiles.find((p) => p.profile_id === e.target.value);
                          if (profile) {
                            onSelectWorker?.(card, profile.worker_type);
                            onSelectProfile?.(card, profile.profile_id);
                          }
                        }}
                        disabled={readOnly || !enabledProfiles.length}
                      >
                        {enabledProfiles.length ? (
                          enabledProfiles.map((profile) => {
                            const authLabel = profile.auth_mode === "cli_native"
                              ? " · CLI login"
                              : " · Project API";
                            const compatibilityLabel = profile.worker_type === "pi" ? " · 最佳兼容" : " · 部分兼容";
                            return (
                              <option key={profile.profile_id} value={profile.profile_id}>
                                {profile.display_name}{authLabel}{compatibilityLabel}
                              </option>
                            );
                          })
                        ) : (
                          <option value="">未配置真实执行器</option>
                        )}
                      </select>
                      {executorCompatibility ? (
                        <span className="executor-compat-note">{executorCompatibility}</span>
                      ) : null}
                    </label>
                    <label className="executor-select-label" onClick={(e) => e.stopPropagation()}>
                      <span>本次运行 Python</span>
                      <span className="effective-runtime-hint">{effectivePythonLabel}</span>
                      <select
                        value={selectedPythonRuntime ?? "__global__"}
                        onChange={(e) => onSelectPythonRuntime?.(card, e.target.value === "__global__" ? undefined : e.target.value)}
                        disabled={readOnly || !pythonRuntimes.length}
                      >
                        <option value="__global__">使用当前生效值</option>
                        {pythonRuntimes.map((item) => (
                          <option key={`${item.manager}:${item.name}`} value={item.name}>
                            {item.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="executor-select-label" onClick={(e) => e.stopPropagation()}>
                      <span>本次运行 R</span>
                      <span className="effective-runtime-hint">{effectiveRLabel}</span>
                      <select
                        value={selectedRRuntime ?? "__global__"}
                        onChange={(e) => onSelectRRuntime?.(card, e.target.value === "__global__" ? undefined : e.target.value)}
                        disabled={readOnly || !rRuntimes.length}
                      >
                        <option value="__global__">使用当前生效值</option>
                        {rRuntimes.map((item) => (
                          <option key={`${item.manager}:${item.name}`} value={item.name}>
                            {item.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button className="btn primary" disabled={readOnly} style={{ width: "100%" }} onClick={(e) => { e.stopPropagation(); onStartRun(card); }}>
                      <Play size={14} /> 开始执行
                    </button>
                  </div>
                ) : null}

                {card.status !== "planned" && card.status !== "running" && card.status !== "reviewing" && card.status !== "proposed" ? (
                  <div className="executor-run-control" style={{ marginTop: 12 }}>
                    <button
                      className="btn secondary"
                      disabled={readOnly}
                      style={{ width: "100%" }}
                      onClick={(e) => { e.stopPropagation(); onRerunCard?.(card, "all"); }}
                      title="重跑本卡片，下游全部标为 stale"
                    >
                      <RotateCcw size={14} /> 重跑本卡（级联下游）
                    </button>
                    <button
                      className="btn primary"
                      disabled={readOnly}
                      style={{ width: "100%", marginTop: 8 }}
                      onClick={(e) => { e.stopPropagation(); onRunSubgraph?.(card); }}
                      title="从本卡片开始，按拓扑序跑到末尾"
                    >
                      <Play size={14} /> 从这里跑到末尾
                    </button>
                  </div>
                ) : null}
              </div>
            </div>

            {/* ─── Page 2: Result ─── */}
            <div className="file-bag-page">
              <div className="page-content-scroll">
                <div style={{ fontSize: 11, fontWeight: 700, marginBottom: 8, color: "var(--text)" }}>RESULT HIGHLIGHTS</div>
                <div style={{ fontSize: 11, color: "var(--text-secondary)", lineHeight: 1.5 }}>
                  {card.key_findings.length ? (
                    <ul style={{ margin: 0, paddingLeft: 14 }}>
                      {card.key_findings.map((f, i) => <li key={i} style={{ marginBottom: 4 }}>{f}</li>)}
                    </ul>
                  ) : <div>暂无关键结果</div>}
                </div>
                {visibleManagerReview ? (
                  <div style={{ fontSize: 11, color: "var(--amber-dark)", marginTop: 8, fontWeight: 500, padding: "6px 8px", background: "var(--amber-bg)", borderRadius: 6 }}>
                    评审: {visibleManagerReview}
                  </div>
                ) : null}
                
                {card.status === "reviewing" ? (
                  <div className="muted" style={{ marginTop: 12, fontSize: 11 }}>
                    Reviewer 正在自动验收，验收通过后会进入 accepted。
                  </div>
                ) : null}
                {card.status === "needs_review" && card.linked_runs.length ? (
                  <button className="btn success" disabled={readOnly} style={{ width: "100%", marginTop: 12 }} onClick={(e) => { e.stopPropagation(); onReviewRun(card); }}>
                    <CheckCircle2 size={14} /> 人工接受旧结果
                  </button>
                ) : null}
              </div>
            </div>

            {/* ─── Page 4: Files ─── */}
            <div className="file-bag-page">
              <div className="page-content-scroll">
                <FileBag
                  projectId={projectId}
                  card={card}
                  embedded
                  mode={isDormant ? "archive" : "files"}
                  onAskManager={onAskManager}
                  onPreviewAsset={onPreviewAsset}
                />
              </div>
            </div>

          </div>
        </div>
      </div>
    </div>
  );
}

function executorCompatibilityCopy(workerType: string): string {
  if (workerType === "pi") {
    return "Pi Agent 具有最佳兼容性，完整支持 Blueprint 的 skill、MCP、tool 和沙箱契约。";
  }
  if (workerType === "opencode") {
    return "OpenCode 为部分兼容：支持执行器选择和项目 API 注入，MCP/tool 以 wrapper 与外层沙箱为主。";
  }
  if (workerType === "claude_code") {
    return "Claude Code 为部分兼容：仅使用本机 CLI 登录态，MCP/tool 做原生能力映射，不注入项目 API。";
  }
  if (workerType === "codex") {
    return "Codex 为部分兼容：仅使用本机 CLI 登录态，MCP/tool 原生映射按能力逐步接入。";
  }
  return "该执行器为部分兼容，请以运行 trace 和沙箱计划为准。";
}
