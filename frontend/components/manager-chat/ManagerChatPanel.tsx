"use client";

import { CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  FileText,
  Loader2,
  Paperclip,
  Pencil,
  Send,
  Square,
  Sparkles,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api, apiUrl, ChatHistoryMessage, ChatRequestContext, ChatStreamEvent, ChatTokenUsage } from "@/lib/api";
import { useChatSession, useModifyProposalMutation } from "@/lib/hooks";
import { queryKeys } from "@/lib/query-keys";
import { Asset, ChatSessionDetail, ChatSessionMessageRecord, ManagerAutoState, ProjectSnapshot, Proposal } from "@/lib/types";
import { Attachment, EMPTY_ATTACHMENTS, useWorkspaceUiStore } from "@/lib/stores/workspace-ui-store";

type ThinkingEffort = "low" | "medium" | "high";
type ToolState = "running" | "done" | "error";
type TimelineItemKind = "thinking" | "tool" | "text" | "compact" | "command";

interface ToolUseState {
  id: string;
  toolName?: string;
  label: string;
  status: ToolState;
}

interface MessageTimelineItem {
  id: string;
  kind: TimelineItemKind;
  content?: string;
  label?: string;
  toolName?: string;
  status?: ToolState | "idle";
  startedAt?: number;
  endedAt?: number;
  firstKeptMessageId?: string;
  tokensBefore?: number;
  tokensAfter?: number;
  durationMs?: number;
  provider?: string;
  model?: string;
}

interface ChatMessage {
  id: string;
  role: "user" | "manager";
  content: string;
  proposal?: Proposal;
  thinking?: string;
  attachments?: Attachment[];
  thinkingState?: "idle" | "running" | "done" | "error";
  tools?: ToolUseState[];
  state?: "idle" | "thinking" | "streaming" | "done" | "error";
  timeline?: MessageTimelineItem[];
  tokenUsage?: ChatTokenUsage;
}

interface ProposalMutationResponse {
  proposal?: Proposal;
  snapshot?: ProjectSnapshot;
}

interface MentionState {
  query: string;
  start: number;
  end: number;
}

interface SlashCommandState {
  query: string;
  start: number;
  end: number;
}

interface UploadProgressState {
  fileName: string;
  fileSize: number;
  loaded: number;
  total?: number;
  percent?: number;
  speedBytesPerSecond?: number;
  status: "uploading" | "processing";
}

interface SlashCommandOption {
  command: string;
  label: string;
}

const MANAGER_CONTEXT_WINDOW_TOKENS = 1_000_000;
const CHARS_PER_TOKEN_ESTIMATE = 3.6;
const PROJECT_MUTATION_TOOLS = /^(create_card|update_card|delete_card|configure_card_execution|start_card_run|rerun_card|review_card_run|stop_card_run|cleanup_run_history|import_card_package|instantiate_card_package)$/;
const RUN_CONTROL_TOOLS = /^(start_card_run|rerun_card|review_card_run|stop_card_run|cleanup_run_history)$/;
const SLASH_COMMANDS: SlashCommandOption[] = [
  { command: "/compact", label: "压缩当前会话上下文" },
  { command: "/auto", label: "启动 /auto <目标>" },
];

const DEFAULT_MANAGER_MESSAGE: ChatMessage = {
  id: "welcome",
  role: "manager",
  state: "done",
  content: "可以先正常聊天和查看上下文；当你明确要求调整蓝图时，我会通过后端工具直接读写 cards，并按数据资产时间线校验。",
  timeline: [
    {
      id: "welcome_text",
      kind: "text",
      content: "可以先正常聊天和查看上下文；当你明确要求调整蓝图时，我会通过后端工具直接读写 cards，并按数据资产时间线校验。",
      status: "done",
    },
  ],
};

function isProposalShape(value: unknown): value is Proposal {
  if (!value || typeof value !== "object") {
    return false;
  }
  const proposal = value as Record<string, unknown>;
  return (
    typeof proposal.proposal_id === "string" &&
    proposal.proposal_id.length > 0 &&
    typeof proposal.patch_id === "string" &&
    typeof proposal.title === "string" &&
    typeof proposal.summary === "string" &&
    typeof proposal.impact_summary === "string" &&
    typeof proposal.status === "string" &&
    Array.isArray(proposal.consistency_warnings) &&
    typeof proposal.created_at === "string" &&
    typeof proposal.updated_at === "string"
  );
}

function parseProposal(value: unknown): Proposal | null {
  if (isProposalShape(value)) {
    return value;
  }
  if (value && typeof value === "object" && "proposal" in value) {
    const nested = (value as { proposal?: unknown }).proposal;
    if (isProposalShape(nested)) {
      return nested;
    }
  }
  return null;
}

function isAbortLikeError(error: unknown): boolean {
  if (!(error instanceof Error)) {
    return false;
  }
  return /abort|aborted|client_disconnected/i.test(`${error.name} ${error.message}`);
}

function settleInterruptedTools(tools?: ToolUseState[]) {
  return (tools ?? []).map((tool) =>
    tool.status === "running"
      ? {
          ...tool,
          status: "error" as const,
        }
      : tool,
  );
}

function settleRunningTimelineItems(
  timeline: MessageTimelineItem[],
  status: Extract<ToolState, "done" | "error">,
) {
  const endedAt = Date.now();
  return timeline.map((item) =>
    item.status === "running"
      ? {
          ...item,
          status,
          endedAt: item.endedAt ?? endedAt,
        }
      : item,
  );
}

function settleCompletedTimelineTools(timeline: MessageTimelineItem[]) {
  return timeline.map((item) =>
    item.kind === "tool" && item.status === "running"
      ? {
          ...item,
          status: "done" as const,
          endedAt: item.endedAt ?? Date.now(),
        }
      : item,
  );
}

function settleRunningTimelineText(timeline: MessageTimelineItem[]) {
  return timeline.map((item) => (item.kind === "text" && item.status === "running" ? { ...item, status: "done" as const } : item));
}

function runtimeForChatContext(runtime?: string | null) {
  if (!runtime || runtime === "__system__") return null;
  return runtime;
}

function estimateTokens(text: string) {
  if (!text) return 0;
  return Math.ceil(text.length / CHARS_PER_TOKEN_ESTIMATE);
}

function normalizeTokenUsage(usage?: ChatTokenUsage | null): ChatTokenUsage | undefined {
  if (!usage) return undefined;
  const numberOrZero = (value: unknown) => (Number.isFinite(Number(value)) ? Number(value) : 0);
  const inputTokens = numberOrZero(usage.input_tokens);
  const outputTokens = numberOrZero(usage.output_tokens);
  const cacheReadTokens = numberOrZero(usage.cache_read_tokens);
  const cacheWriteTokens = numberOrZero(usage.cache_write_tokens);
  const totalTokens =
    numberOrZero(usage.total_tokens) || inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens;
  if (totalTokens <= 0) return undefined;
  return {
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    cache_read_tokens: cacheReadTokens,
    cache_write_tokens: cacheWriteTokens,
    total_tokens: totalTokens,
    context_window_tokens: usage.context_window_tokens ?? null,
    max_output_tokens: usage.max_output_tokens ?? null,
  };
}

function formatElapsedTime(startedAt?: number, endedAt?: number) {
  if (!startedAt) return "";
  const end = endedAt ?? Date.now();
  const totalSeconds = Math.max(0, Math.round((end - startedAt) / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes > 0) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

function isNearBottom(element: HTMLElement, threshold = 48) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function formatUploadBytes(size: number) {
  if (!Number.isFinite(size) || size <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const digits = value >= 10 || index === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[index]}`;
}

function formatUploadSpeed(speed?: number) {
  if (!speed || speed <= 0) return "计算速度中";
  return `${formatUploadBytes(speed)}/s`;
}

function upsertTimelineItem(
  timeline: MessageTimelineItem[],
  item: MessageTimelineItem,
  matcher?: (candidate: MessageTimelineItem) => boolean,
): MessageTimelineItem[] {
  const next = [...timeline];
  const index = matcher ? next.findIndex(matcher) : -1;
  if (index >= 0) {
    next[index] = { ...next[index], ...item };
    return next;
  }
  next.push(item);
  return next;
}

function timelineItemId(kind: TimelineItemKind, index: number | undefined, fallback: string, turnIndex?: number) {
  const turnPrefix = turnIndex === undefined ? "" : `${turnIndex}_`;
  return `${kind}_${turnPrefix}${index ?? fallback}`;
}

function parseSlashCommand(message: string): { isCmd: boolean; cmdType?: string; objective?: string } {
  const normalized = message.trim();
  const match = normalized.match(/^\/auto(?:[ \t]+([^\r\n]*))?$/);
  if (!match) {
    return { isCmd: false };
  }
  const arg = match[1]?.trim();
  if (!arg) {
    return { isCmd: true, cmdType: "bare" };
  }
  if (arg === "off" || arg === "stop") {
    return { isCmd: true, cmdType: "stop", objective: arg };
  } else if (arg === "status") {
    return { isCmd: true, cmdType: "status", objective: arg };
  } else if (arg === "once") {
    return { isCmd: true, cmdType: "once", objective: arg };
  } else {
    return { isCmd: true, cmdType: "enable", objective: arg };
  }
}

function lastTimelineIndex(timeline: MessageTimelineItem[], predicate: (item: MessageTimelineItem) => boolean) {
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    if (predicate(timeline[index])) return index;
  }
  return -1;
}

function hasVisibleTimelineContent(timeline?: Array<MessageTimelineItem | NonNullable<ChatSessionMessageRecord["timeline"]>[number]> | null) {
  return Boolean(
    timeline?.some((item) => {
      if (!item) return false;
      const content = "content" in item ? item.content : undefined;
      const label = "label" in item ? item.label : undefined;
      const toolName = "toolName" in item ? item.toolName : "tool_name" in item ? item.tool_name : undefined;
      return Boolean(String(content ?? label ?? toolName ?? "").trim());
    }),
  );
}

function isEmptyPendingManagerSessionMessage(message: ChatSessionMessageRecord) {
  return (
    message.role === "manager" &&
    message.state !== "done" &&
    message.state !== "error" &&
    !String(message.content ?? "").trim() &&
    !String(message.thinking ?? "").trim() &&
    !hasVisibleTimelineContent(message.timeline)
  );
}

function isEmptyPendingManagerMessage(message: ChatMessage) {
  return (
    message.role === "manager" &&
    message.state !== "done" &&
    message.state !== "error" &&
    !message.content.trim() &&
    !String(message.thinking ?? "").trim() &&
    !hasVisibleTimelineContent(message.timeline)
  );
}

function isPendingManagerSessionMessage(message: ChatSessionMessageRecord) {
  return message.role === "manager" && message.state !== "done" && message.state !== "error";
}

function normalizeSessionMessages(
  messages: ChatSessionMessageRecord[],
  options: { settleStaleManagerMessages?: boolean } = {},
): ChatMessage[] {
  return messages
    .filter((message) => Boolean(message?.id) && (message.role === "user" || message.role === "manager") && !isEmptyPendingManagerSessionMessage(message))
    .map((message) => {
      const settleStaleManagerMessage = Boolean(options.settleStaleManagerMessages && isPendingManagerSessionMessage(message));
      const timeline: MessageTimelineItem[] = message.timeline?.length
        ? message.timeline.map((item) => ({
            id: item.id,
            kind: item.kind as TimelineItemKind,
            content: item.content ?? undefined,
            label: item.label ?? undefined,
            toolName: item.tool_name ?? undefined,
            status: settleStaleManagerMessage && item.status === "running" ? "done" : (item.status as ToolState | "idle" | undefined) ?? undefined,
            startedAt: item.started_at ?? undefined,
            endedAt: settleStaleManagerMessage && item.status === "running" ? item.ended_at ?? Date.now() : item.ended_at ?? undefined,
            firstKeptMessageId: item.first_kept_message_id ?? undefined,
            tokensBefore: item.tokens_before ?? undefined,
            tokensAfter: item.tokens_after ?? undefined,
            durationMs: item.duration_ms ?? undefined,
            provider: item.provider ?? undefined,
            model: item.model ?? undefined,
          }))
        : [];
      if (!timeline.length) {
        if (message.thinking) {
          timeline.push({
            id: `${message.id}_thinking`,
            kind: "thinking",
            content: message.thinking,
            status: "done",
          });
        }
        if (message.content) {
          timeline.push({
            id: `${message.id}_text`,
            kind: "text",
            content: message.content,
            status: "done",
          });
        }
      }
      return {
        id: message.id,
        role: message.role,
        content: message.content,
        proposal: message.proposal ?? undefined,
        thinking: message.thinking ?? undefined,
        attachments: message.attachments ?? [],
        state: settleStaleManagerMessage ? "done" : message.state ?? "done",
        thinkingState: message.thinking || settleStaleManagerMessage ? "done" : "idle",
        timeline,
        tokenUsage: normalizeTokenUsage(message.token_usage),
      };
    });
}

function mergeChatMessagesById(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const cleanCurrent = current.filter((message) => !isEmptyPendingManagerMessage(message));
  const cleanIncoming = incoming.filter((message) => !isEmptyPendingManagerMessage(message));
  const merged = new Map(cleanCurrent.map((message) => [message.id, message]));
  for (const message of cleanIncoming) {
    const existing = merged.get(message.id);
    merged.set(
      message.id,
      existing
        ? {
            ...existing,
            ...message,
            attachments: message.attachments ?? existing.attachments,
            tools: message.tools ?? existing.tools,
            timeline: message.timeline?.length ? message.timeline : existing.timeline,
            tokenUsage: message.tokenUsage ?? existing.tokenUsage,
          }
        : message,
    );
  }
  const ordered: ChatMessage[] = [];
  for (const message of cleanCurrent) {
    ordered.push(merged.get(message.id) ?? message);
  }
  for (const message of cleanIncoming) {
    if (!cleanCurrent.some((item) => item.id === message.id)) {
      ordered.push(message);
    }
  }
  return ordered.filter((message, index, list) => list.findIndex((item) => item.id === message.id) === index);
}

function serializeSessionMessages(messages: ChatMessage[]): ChatSessionMessageRecord[] {
  return messages
    .filter((message) => !isEmptyPendingManagerMessage(message))
    .map((message) => ({
    id: message.id,
    role: message.role,
    content: message.content,
    proposal: message.proposal ?? null,
    thinking: message.thinking ?? null,
    attachments: message.attachments ?? [],
    state: message.state ?? "done",
    timeline: message.timeline?.length
      ? message.timeline.map((item) => ({
          id: item.id,
          kind: item.kind,
          content: item.content ?? null,
          label: item.label ?? null,
          tool_name: item.toolName ?? null,
          status: item.status ?? null,
          started_at: item.startedAt ?? null,
          ended_at: item.endedAt ?? null,
          first_kept_message_id: item.firstKeptMessageId ?? null,
          tokens_before: item.tokensBefore ?? null,
          tokens_after: item.tokensAfter ?? null,
          duration_ms: item.durationMs ?? null,
          provider: item.provider ?? null,
          model: item.model ?? null,
        }))
      : null,
    token_usage: message.tokenUsage ?? null,
  }));
}

function compactTimelineSummary(message: ChatMessage): string | null {
  const item = message.timeline?.find((candidate) => candidate.kind === "compact" && candidate.content?.trim());
  return item?.content?.trim() || null;
}

function toHistoryContent(message: ChatMessage): string {
  const compactSummary = compactTimelineSummary(message);
  if (compactSummary) {
    return `Context summary:\n${compactSummary}`;
  }
  return message.content.trim();
}

function sessionMessagesSignature(messages: ChatMessage[]): string {
  return JSON.stringify(
    messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      proposal_id: message.proposal?.proposal_id ?? null,
      proposal_status: message.proposal?.status ?? null,
      thinking: message.thinking ?? null,
      attachments: message.attachments ?? [],
      state: message.state ?? null,
      token_usage: message.tokenUsage
        ? {
            total_tokens: message.tokenUsage.total_tokens,
            context_window_tokens: message.tokenUsage.context_window_tokens ?? null,
          }
        : null,
      timeline: message.timeline?.map((item) => ({
        id: item.id,
        kind: item.kind,
        content: item.content ?? null,
        label: item.label ?? null,
        toolName: item.toolName ?? null,
        status: item.status,
        startedAt: item.startedAt ?? null,
        endedAt: item.endedAt ?? null,
      })) ?? [],
    })),
  );
}

function upsertSessionSummary(
  items: ChatSessionDetail[] | Array<{ session_id: string; summary: string; created_at: string; updated_at: string; message_count: number }> | undefined,
  session: ChatSessionDetail,
) {
  const next = [...(items ?? [])];
  const summary = {
    session_id: session.session_id,
    summary: session.summary,
    created_at: session.created_at,
    updated_at: session.updated_at,
    message_count: session.messages.length,
  };
  const index = next.findIndex((item) => item.session_id === session.session_id);
  if (index >= 0) {
    next[index] = summary;
  } else {
    next.push(summary);
  }
  return next.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

export function ManagerChatPanel({
  projectId,
  sessionId,
  managerAuto,
  proposals = [],
  mentionableAssets,
  onRefresh,
}: {
  projectId: string;
  sessionId?: string | null;
  managerAuto?: ManagerAutoState;
  proposals?: Proposal[];
  mentionableAssets: Asset[];
  onRefresh: () => Promise<void>;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [thinkingEffort, setThinkingEffort] = useState<ThinkingEffort>("medium");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [localManagerAuto, setLocalManagerAuto] = useState<ManagerAutoState | null>(managerAuto ?? null);
  const [editingProposalId, setEditingProposalId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [mentionState, setMentionState] = useState<MentionState | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [slashCommandState, setSlashCommandState] = useState<SlashCommandState | null>(null);
  const [slashCommandIndex, setSlashCommandIndex] = useState(0);
  const [effortMenuOpen, setEffortMenuOpen] = useState(false);
  const [autoStopPending, setAutoStopPending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const effortMenuRef = useRef<HTMLDivElement>(null);
  const thinkingRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const refreshTimerRef = useRef<number | null>(null);
  const delayedRefreshTimerRef = useRef<number | null>(null);
  const asyncBoundaryPollRef = useRef<number | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const autoSessionReconnectTimerRef = useRef<number | null>(null);
  const activeStreamControllerRef = useRef<AbortController | null>(null);
  const autoSessionEventSourceRef = useRef<EventSource | null>(null);
  const activeAutoStreamMessagesRef = useRef<Set<string>>(new Set());
  const autoStreamSeqRef = useRef<Map<string, number>>(new Map());
  const shouldStickToBottomRef = useRef(true);
  const remoteHydratingRef = useRef(false);
  const stopRequestedRef = useRef(false);
  const currentSessionKeyRef = useRef(`${projectId}:${sessionId ?? ""}`);
  const currentSessionIdRef = useRef<string | null>(sessionId ?? null);
  const hydratedSessionIdRef = useRef<string | null>(null);
  const lastSavedSignatureRef = useRef("[]");
  const sessionRevisionRef = useRef(0);
  const effectiveManagerAuto = localManagerAuto ?? managerAuto;
  const isAutoOwnerSession = Boolean(effectiveManagerAuto?.enabled && sessionId && effectiveManagerAuto.owner_session_id === sessionId);
  const isBtwSession = Boolean(effectiveManagerAuto?.enabled && sessionId && effectiveManagerAuto.owner_session_id && effectiveManagerAuto.owner_session_id !== sessionId);
  const autoScopedActive = Boolean(isAutoOwnerSession && effectiveManagerAuto?.enabled);
  const autoComposerState = !autoScopedActive ? "normal" : "auto_running";
  const [uploadProgress, setUploadProgress] = useState<UploadProgressState | null>(null);
  const uploadProgressRef = useRef({ loaded: 0, at: 0, speedBytesPerSecond: undefined as number | undefined });
  const uploadAbortControllerRef = useRef<AbortController | null>(null);
  const chatSessionQuery = useChatSession(projectId, sessionId ?? undefined, Boolean(sessionId), {
    refetchInterval: effectiveManagerAuto?.enabled && !activeStreamControllerRef.current ? 4_000 : false,
  });
  const refetchChatSession = chatSessionQuery.refetch;

  const attachments = useWorkspaceUiStore((s) => s.attachmentsByProject[projectId] ?? EMPTY_ATTACHMENTS);
  const scriptPreference = useWorkspaceUiStore((s) => s.scriptPreferenceByProject?.[projectId] ?? "auto");
  const globalPythonRuntime = useWorkspaceUiStore((s) => s.globalPythonRuntimeByProject?.[projectId]);
  const globalRRuntime = useWorkspaceUiStore((s) => s.globalRRuntimeByProject?.[projectId]);
  const addAttachment = useWorkspaceUiStore((s) => s.addAttachment);
  const removeAttachment = useWorkspaceUiStore((s) => s.removeAttachment);
  const clearAttachments = useWorkspaceUiStore((s) => s.clearAttachments);
  const draftMessage = useWorkspaceUiStore((s) => s.draftMessageByProject[projectId] ?? "");
  const clearDraftMessage = useWorkspaceUiStore((s) => s.clearDraftMessage);
  const saveSessionMutation = useMutation({
    mutationFn: ({
      targetSessionId,
      nextMessages,
    }: {
      targetSessionId: string;
      nextMessages: ChatMessage[];
    }) =>
      api.saveChatSession(projectId, targetSessionId, serializeSessionMessages(nextMessages), undefined, sessionRevisionRef.current),
    onSuccess: ({ session }, variables) => {
      if (currentSessionIdRef.current !== variables.targetSessionId) {
        return;
      }
      sessionRevisionRef.current = session.revision;
      lastSavedSignatureRef.current = sessionMessagesSignature(variables.nextMessages);
      queryClient.setQueryData(queryKeys.chatSession(projectId, session.session_id), { session });
      queryClient.setQueryData(
        queryKeys.chatSessions(projectId),
        (
          previous:
            | { items: Array<{ session_id: string; summary: string; created_at: string; updated_at: string; message_count: number }> }
            | undefined,
        ) => ({
          items: upsertSessionSummary(previous?.items, session),
        }),
      );
    },
    onError: (nextError, variables) => {
      const message = nextError instanceof Error ? nextError.message : "Session save failed.";
      const staleSessionSave =
        currentSessionIdRef.current !== variables.targetSessionId ||
        /Chat session not found/i.test(message);
      if (staleSessionSave) {
        return;
      }
      setError(message);
    },
  });

  useEffect(() => {
    setLocalManagerAuto(managerAuto ?? null);
  }, [managerAuto]);

  function applyManagerAutoState(state: ManagerAutoState) {
    setLocalManagerAuto(state);
    queryClient.setQueryData(queryKeys.managerAuto(projectId, sessionId), {
      state,
      is_owner: Boolean(sessionId && state.enabled && state.owner_session_id === sessionId),
      btw_mode: Boolean(sessionId && state.enabled && state.owner_session_id && state.owner_session_id !== sessionId),
    });
    queryClient.setQueryData<ProjectSnapshot>(queryKeys.project(projectId), (previous) =>
      previous ? { ...previous, manager_auto: state } : previous,
    );
  }
  const uploadMutation = useMutation({
    mutationFn: (file: File) => {
      uploadAbortControllerRef.current?.abort();
      const controller = new AbortController();
      uploadAbortControllerRef.current = controller;
      uploadProgressRef.current = { loaded: 0, at: performance.now(), speedBytesPerSecond: undefined };
      setUploadProgress({
        fileName: file.name,
        fileSize: file.size,
        loaded: 0,
        total: file.size || undefined,
        percent: file.size ? 0 : undefined,
        status: "uploading",
      });
      return api.uploadChatFileWithProgress(projectId, file, (event) => {
        const now = performance.now();
        const previous = uploadProgressRef.current;
        const elapsedSeconds = Math.max((now - previous.at) / 1000, 0.001);
        const instantSpeed = event.loaded > previous.loaded ? (event.loaded - previous.loaded) / elapsedSeconds : undefined;
        const speedBytesPerSecond =
          instantSpeed && previous.speedBytesPerSecond
            ? previous.speedBytesPerSecond * 0.72 + instantSpeed * 0.28
            : instantSpeed ?? previous.speedBytesPerSecond;
        uploadProgressRef.current = { loaded: event.loaded, at: now, speedBytesPerSecond };
        const total = event.total ?? file.size;
        setUploadProgress({
          fileName: file.name,
          fileSize: file.size,
          loaded: event.loaded,
          total: total || undefined,
          percent: event.lengthComputable && total ? Math.min(100, Math.round((event.loaded / total) * 100)) : undefined,
          speedBytesPerSecond,
          status: event.lengthComputable && total && event.loaded >= total ? "processing" : "uploading",
        });
      }, controller.signal);
    },
    onSuccess: async (response) => {
      addAttachment(projectId, response.attachment);
      setError(null);
      setUploadProgress((current) =>
        current
          ? {
              ...current,
              loaded: current.total ?? current.fileSize,
              percent: current.total || current.fileSize ? 100 : current.percent,
              status: "processing",
            }
          : current,
      );
      queryClient.invalidateQueries({ queryKey: queryKeys.files(projectId) });
      await onRefresh();
      setUploadProgress(null);
    },
    onError: (nextError) => {
      if (nextError instanceof DOMException && nextError.name === "AbortError") {
        setUploadProgress(null);
        return;
      }
      setError(nextError instanceof Error ? nextError.message : "Upload failed.");
      setUploadProgress(null);
    },
    onSettled: () => {
      uploadAbortControllerRef.current = null;
    },
  });
  const acceptProposalMutation = useMutation({
    mutationFn: (proposalId: string) => api.acceptProposal(projectId, proposalId, sessionId),
  });
  const rejectProposalMutation = useMutation({
    mutationFn: (proposalId: string) => api.rejectProposal(projectId, proposalId),
  });
  const modifyProposalMutation = useModifyProposalMutation(projectId);

  useEffect(() => {
    return () => {
      uploadAbortControllerRef.current?.abort();
    };
  }, [projectId, sessionId]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    shouldStickToBottomRef.current = true;
    const handleScroll = () => {
      shouldStickToBottomRef.current = isNearBottom(element);
    };
    handleScroll();
    element.addEventListener("scroll", handleScroll, { passive: true });
    return () => element.removeEventListener("scroll", handleScroll);
  }, [sessionId]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !shouldStickToBottomRef.current) {
      return;
    }
    element.scrollTop = element.scrollHeight;
  }, [sessionId, messages, error, busy, attachments, chatSessionQuery.isLoading]);

  useEffect(() => {
    if (draftMessage) {
      setDraft((prev) => (prev ? prev + "\n" : "") + draftMessage);
      clearDraftMessage(projectId);
    }
  }, [draftMessage, projectId, clearDraftMessage]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 144)}px`;
  }, [draft]);

  useEffect(() => {
    if (!error || typeof window === "undefined") {
      return;
    }
    const timer = window.setTimeout(() => {
      setError(null);
    }, 3200);
    return () => window.clearTimeout(timer);
  }, [error]);

  useEffect(() => {
    setMentionIndex(0);
  }, [mentionState?.query]);

  useEffect(() => {
    setSlashCommandIndex(0);
  }, [slashCommandState?.query]);

  useEffect(() => {
    if (!effortMenuOpen) return;
    function handlePointerDown(event: MouseEvent) {
      if (!effortMenuRef.current?.contains(event.target as Node)) {
        setEffortMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [effortMenuOpen]);

  useEffect(() => {
    const nextSessionKey = `${projectId}:${sessionId ?? ""}`;
    const sessionChanged = currentSessionKeyRef.current !== nextSessionKey;
    currentSessionKeyRef.current = nextSessionKey;
    currentSessionIdRef.current = sessionId ?? null;
    if (sessionChanged) {
      activeStreamControllerRef.current?.abort(new Error("session_changed"));
      activeStreamControllerRef.current = null;
      stopRequestedRef.current = false;
    }
    if (saveTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    if (refreshTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
    if (delayedRefreshTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(delayedRefreshTimerRef.current);
      delayedRefreshTimerRef.current = null;
    }
    if (asyncBoundaryPollRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(asyncBoundaryPollRef.current);
      asyncBoundaryPollRef.current = null;
    }
    if (sessionChanged) {
      activeAutoStreamMessagesRef.current.clear();
      autoStreamSeqRef.current.clear();
      hydratedSessionIdRef.current = null;
      lastSavedSignatureRef.current = "[]";
      setBusy(false);
      setError(null);
      setEditingProposalId(null);
      setEditDraft("");
      setMentionState(null);
      setMentionIndex(0);
      setSlashCommandState(null);
      setSlashCommandIndex(0);
      setEffortMenuOpen(false);
      setMessages([]);
    }
  }, [projectId, sessionId]);

  useEffect(() => {
    if (!sessionId || !chatSessionQuery.data?.session || activeStreamControllerRef.current) {
      return;
    }
    const nextMessages = normalizeSessionMessages(
      chatSessionQuery.data.session.messages.filter((message) => !shouldIgnoreIncomingSessionMessage(message)),
      { settleStaleManagerMessages: true },
    );
    const mergedMessages = mergeChatMessagesById(messages, nextMessages);
    const nextSignature = sessionMessagesSignature(mergedMessages);
    sessionRevisionRef.current = chatSessionQuery.data.session.revision;
    if (hydratedSessionIdRef.current === sessionId && nextSignature === lastSavedSignatureRef.current) {
      lastSavedSignatureRef.current = nextSignature;
      return;
    }
    hydratedSessionIdRef.current = sessionId;
    lastSavedSignatureRef.current = nextSignature;
    // Set remote-hydration guard so the save effect does not immediately
    // PUT server-normalized messages back — this prevents the hydrate/save
    // loop when the only change is server-side normalization.
    remoteHydratingRef.current = true;
    setMessages(mergedMessages);
    setError(null);
  }, [chatSessionQuery.data, messages, sessionId]);

  useEffect(() => {
    if (!sessionId || hydratedSessionIdRef.current !== sessionId || typeof window === "undefined") {
      return;
    }
    if (remoteHydratingRef.current) {
      remoteHydratingRef.current = false;
      return;
    }
    const signature = sessionMessagesSignature(messages);
    if (signature === lastSavedSignatureRef.current) {
      return;
    }
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
    }
    saveTimerRef.current = window.setTimeout(() => {
      saveTimerRef.current = null;
      saveSessionMutation.mutate({ targetSessionId: sessionId, nextMessages: messages });
    }, 600);
    return () => {
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current);
        saveTimerRef.current = null;
      }
    };
  }, [messages, projectId, saveSessionMutation, sessionId]);

  useEffect(() => {
    autoSessionEventSourceRef.current?.close();
    autoSessionEventSourceRef.current = null;
    activeAutoStreamMessagesRef.current.clear();
    autoStreamSeqRef.current.clear();
    if (autoSessionReconnectTimerRef.current !== null) {
      window.clearTimeout(autoSessionReconnectTimerRef.current);
      autoSessionReconnectTimerRef.current = null;
    }
    if (!sessionId || !isAutoOwnerSession || !effectiveManagerAuto?.enabled || typeof window === "undefined") {
      return;
    }
    let stopped = false;
    let reconnectAttempt = 0;

    const connect = () => {
      if (stopped) return;
      const source = new EventSource(apiUrl(`/projects/${projectId}/chat-sessions/${sessionId}/events`));
      autoSessionEventSourceRef.current = source;
      source.onopen = () => {
        reconnectAttempt = 0;
      };
      source.onmessage = (event) => {
        if (!event.data) return;
        const payload = JSON.parse(event.data) as {
          type?: string;
          message?: ChatSessionMessageRecord;
          message_id?: string;
          event?: ChatStreamEvent;
          revision?: number;
          seq?: number;
        };
        if (payload.type === "message_upsert") {
          void queryClient.refetchQueries({ queryKey: queryKeys.managerAuto(projectId, sessionId), type: "active" });
        }
        if (payload.type === "stream_event" && payload.message_id && payload.event) {
          if (typeof payload.seq === "number") {
            const lastSeq = autoStreamSeqRef.current.get(payload.message_id) ?? 0;
            if (payload.seq <= lastSeq) {
              return;
            }
            autoStreamSeqRef.current.set(payload.message_id, payload.seq);
          }
          remoteHydratingRef.current = true;
          if (applyCompactStreamEvent(payload.event)) {
            return;
          }
          if (payload.event.type === "done" || payload.event.type === "error") {
            activeAutoStreamMessagesRef.current.delete(payload.message_id);
            autoStreamSeqRef.current.delete(payload.message_id);
          } else {
            activeAutoStreamMessagesRef.current.add(payload.message_id);
          }
          applyStreamEvent(payload.message_id, payload.event);
          return;
        }
        if (payload.type !== "message_upsert" || !payload.message) {
          return;
        }
        if (shouldIgnoreIncomingSessionMessage(payload.message)) {
          return;
        }
        if (payload.message.state === "done" || payload.message.state === "error") {
          activeAutoStreamMessagesRef.current.delete(payload.message.id);
          autoStreamSeqRef.current.delete(payload.message.id);
        }
        const incoming = normalizeSessionMessages([payload.message]);
        if (!incoming.length) return;
        remoteHydratingRef.current = true;
        if (typeof payload.revision === "number") {
          sessionRevisionRef.current = payload.revision;
        }
        setMessages((current) => {
          const merged = mergeChatMessagesById(current, incoming);
          lastSavedSignatureRef.current = sessionMessagesSignature(merged);
          return merged;
        });
      };
      source.onerror = () => {
        source.close();
        if (autoSessionEventSourceRef.current === source) {
          autoSessionEventSourceRef.current = null;
        }
        activeAutoStreamMessagesRef.current.clear();
        autoStreamSeqRef.current.clear();
        if (stopped) return;
        refetchChatSession();
        const delay = Math.min(10_000, 1_000 * 2 ** reconnectAttempt);
        reconnectAttempt += 1;
        autoSessionReconnectTimerRef.current = window.setTimeout(() => {
          autoSessionReconnectTimerRef.current = null;
          connect();
        }, delay);
      };
    };
    connect();
    return () => {
      stopped = true;
      if (autoSessionReconnectTimerRef.current !== null) {
        window.clearTimeout(autoSessionReconnectTimerRef.current);
        autoSessionReconnectTimerRef.current = null;
      }
      autoSessionEventSourceRef.current?.close();
      autoSessionEventSourceRef.current = null;
      activeAutoStreamMessagesRef.current.clear();
      autoStreamSeqRef.current.clear();
    };
  }, [effectiveManagerAuto?.enabled, isAutoOwnerSession, projectId, queryClient, refetchChatSession, sessionId]);

  useEffect(() => () => {
    if (saveTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(saveTimerRef.current);
    }
    if (refreshTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(refreshTimerRef.current);
    }
    if (delayedRefreshTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(delayedRefreshTimerRef.current);
    }
    if (asyncBoundaryPollRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(asyncBoundaryPollRef.current);
    }
    if (autoSessionReconnectTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(autoSessionReconnectTimerRef.current);
    }
    activeStreamControllerRef.current?.abort();
    autoSessionEventSourceRef.current?.close();
  }, []);

  useEffect(() => {
    messages.forEach((message) => {
      if (message.role !== "manager") return;
      (message.timeline ?? [])
        .filter((item) => item.kind === "thinking" && item.content)
        .forEach((item) => {
          const element = thinkingRefs.current[item.id];
          if (element) {
            element.scrollTop = element.scrollHeight;
          }
        });
    });
  }, [messages]);

  function createMessageId() {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
      return crypto.randomUUID();
    }
    return `msg_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  }

  const mentionOptions = useMemo(() => {
    const query = mentionState?.query.trim().toLowerCase() ?? "";
    // Keep the picker audit-friendly for now: users may explicitly reference stale or superseded assets in chat.
    return mentionableAssets
      .filter((asset) => !query || asset.title.toLowerCase().includes(query) || asset.asset_id.toLowerCase().includes(query))
      .sort((left, right) => left.title.localeCompare(right.title))
      .slice(0, 8);
  }, [mentionState?.query, mentionableAssets]);

  const slashCommandOptions = useMemo(() => {
    const query = slashCommandState?.query.trim().toLowerCase() ?? "";
    return SLASH_COMMANDS.filter(
      (item) =>
        !query ||
        item.command.toLowerCase().includes(query) ||
        item.label.toLowerCase().includes(query),
    );
  }, [slashCommandState?.query]);

  function syncMentionState(text: string, selectionStart: number | null) {
    setMentionState(getMentionState(text, selectionStart ?? text.length));
  }

  function syncComposerState(text: string, selectionStart: number | null) {
    const cursor = selectionStart ?? text.length;
    const nextSlashState = getSlashCommandState(text, cursor);
    setSlashCommandState(nextSlashState);
    setMentionState(nextSlashState ? null : getMentionState(text, cursor));
  }

  function insertMention(asset: Asset) {
    const activeMention = mentionState;
    const current = textareaRef.current;
    if (!activeMention || !current) {
      addAttachment(projectId, { type: "asset", id: asset.asset_id, label: asset.title });
      return;
    }
    const nextDraft = `${draft.slice(0, activeMention.start)}@${asset.title} ${draft.slice(activeMention.end)}`;
    const nextCursor = activeMention.start + asset.title.length + 2;
    setDraft(nextDraft);
    addAttachment(projectId, { type: "asset", id: asset.asset_id, label: asset.title });
    setMentionState(null);
    setMentionIndex(0);
    window.requestAnimationFrame(() => {
      current.focus();
      current.setSelectionRange(nextCursor, nextCursor);
    });
  }

  function insertSlashCommand(option: SlashCommandOption) {
    const activeSlash = slashCommandState;
    const current = textareaRef.current;
    if (!activeSlash || !current) {
      setDraft(option.command);
      window.requestAnimationFrame(() => {
        current?.focus();
      });
      return;
    }
    const nextDraft = `${draft.slice(0, activeSlash.start)}${option.command}${draft.slice(activeSlash.end)}`;
    const nextCursor = activeSlash.start + option.command.length;
    setDraft(nextDraft);
    setSlashCommandState(null);
    setSlashCommandIndex(0);
    window.requestAnimationFrame(() => {
      current.focus();
      current.setSelectionRange(nextCursor, nextCursor);
    });
  }

  function updateMessage(messageId: string, updater: (message: ChatMessage) => ChatMessage) {
    setMessages((previous) => previous.map((message) => (message.id === messageId ? updater(message) : message)));
  }

  function updateOrInsertStreamMessage(messageId: string, updater: (message: ChatMessage) => ChatMessage) {
    setMessages((previous) => {
      const hasMessage = previous.some((message) => message.id === messageId);
      const base = hasMessage
        ? previous
        : [
            ...previous,
            {
              id: messageId,
              role: "manager" as const,
              content: "",
              thinking: "",
              thinkingState: "idle" as const,
              tools: [],
              state: "thinking" as const,
              timeline: [],
            },
          ];
      return base.map((message) => (message.id === messageId ? updater(message) : message));
    });
  }

  function buildChatHistory(sourceMessages: ChatMessage[]): ChatHistoryMessage[] {
    return sourceMessages
      .filter((message) => message.role === "user" || message.role === "manager")
      .filter((message) => !message.id.startsWith("cmd_") && !message.timeline?.some((item) => (item.kind as string) === "command"))
      .filter((message) => !message.id.startsWith("wake_notice_"))
      .map((message) => ({
        role: message.role,
        content: toHistoryContent(message),
      }))
      .filter((message) => message.content);
  }

  function findMessageIndexByTimelineId(sourceMessages: ChatMessage[], itemId?: string) {
    if (!itemId) return -1;
    return sourceMessages.findIndex(
      (message) => message.id === itemId || Boolean(message.timeline?.some((item) => item.id === itemId)),
    );
  }

  function buildCompactMessage(item: MessageTimelineItem): ChatMessage {
    return {
      id: `compact_msg_${item.id}`,
      role: "manager",
      content: "",
      state: item.status === "error" ? "error" : "done",
      thinkingState: "idle",
      timeline: [item],
    };
  }

  function upsertCompactMessage(item: MessageTimelineItem) {
    setMessages((previous) => {
      const next = [...previous];
      const messageId = `compact_msg_${item.id}`;
      const existingIndex = next.findIndex((message) => message.id === messageId);
      if (existingIndex >= 0) {
        next[existingIndex] = {
          ...next[existingIndex],
          state: item.status === "error" ? "error" : "done",
          timeline: [item],
        };
        return next;
      }
      const insertIndex = next.findIndex((message) => message.state !== "done");
      next.splice(insertIndex >= 0 ? insertIndex : next.length, 0, buildCompactMessage(item));
      return next;
    });
  }

  function finalizeCompaction(item: MessageTimelineItem) {
    setMessages((previous) => {
      let retainedMessages = previous;
      if (item.firstKeptMessageId === "root") {
        const activeIndex = previous.findIndex((message) => message.state !== "done");
        retainedMessages = activeIndex >= 0 ? previous.slice(Math.max(0, activeIndex - 1)) : [];
      } else {
        const retainedIndex = findMessageIndexByTimelineId(previous, item.firstKeptMessageId);
        if (retainedIndex < 0) {
          retainedMessages = previous;
        } else {
        const matchedMessage = previous[retainedIndex];
        const matchedCompact = matchedMessage.timeline?.some((timelineItem) => timelineItem.id === item.firstKeptMessageId && timelineItem.kind === "compact");
        retainedMessages = previous.slice(matchedCompact ? retainedIndex + 1 : retainedIndex);
      }
      }
      const filtered = retainedMessages.filter((message) => !message.id.startsWith("compact_msg_"));
      return [buildCompactMessage(item), ...filtered];
    });
  }

  function shouldIgnoreIncomingSessionMessage(message: ChatSessionMessageRecord) {
    if (!activeAutoStreamMessagesRef.current.has(message.id)) {
      return false;
    }
    // While the EventSource is continuous, live stream deltas are newer than
    // persisted snapshots. On reconnect the active set is cleared so the next
    // session snapshot can repair a partial message.
    return message.state !== "done" && message.state !== "error";
  }

  function refetchWorkspaceState(runId?: string | null) {
    const queries = [
      queryClient.refetchQueries({ queryKey: queryKeys.project(projectId), type: "active" }),
      queryClient.refetchQueries({ queryKey: queryKeys.workOrder(projectId), type: "active" }),
      queryClient.refetchQueries({ queryKey: queryKeys.advancedProposals(projectId), type: "active" }),
      queryClient.refetchQueries({ queryKey: queryKeys.results(projectId), type: "active" }),
    ];
    if (runId) {
      queries.push(queryClient.refetchQueries({ queryKey: queryKeys.runEvents(projectId, runId), type: "active" }));
    }
    void Promise.all(queries);
  }

  function schedulePartialRefresh(runId?: string | null) {
    if (refreshTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(refreshTimerRef.current);
    }
    if (typeof window === "undefined") {
      return;
    }
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = null;
      refetchWorkspaceState(runId);
    }, 120);
  }

  function scheduleDelayedPartialRefresh(runId?: string | null) {
    if (delayedRefreshTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(delayedRefreshTimerRef.current);
    }
    if (typeof window === "undefined") {
      return;
    }
    delayedRefreshTimerRef.current = window.setTimeout(() => {
      delayedRefreshTimerRef.current = null;
      refetchWorkspaceState(runId);
    }, 1_000);
  }

  function scheduleAsyncBoundaryPoll(jobId: string | null) {
    if (isAutoOwnerSession) return;
    if (typeof window === "undefined") return;
    if (asyncBoundaryPollRef.current !== null) {
      window.clearTimeout(asyncBoundaryPollRef.current);
      asyncBoundaryPollRef.current = null;
    }
    if (!jobId) return;
    const targetId = `depjob_terminal_${jobId}`;
    let attempts = 0;
    const poll = () => {
      attempts += 1;
      refetchChatSession().then((result) => {
        const msgs = result.data?.session?.messages ?? [];
        const hasTerminal = msgs.some((m: { id: string }) => m.id === targetId);
        if (hasTerminal) {
          asyncBoundaryPollRef.current = null;
          return;
        }
        if (attempts >= 30) {
          asyncBoundaryPollRef.current = null;
          return;
        }
        asyncBoundaryPollRef.current = window.setTimeout(poll, 15_000);
      }).catch(() => {
        if (attempts >= 30) {
          asyncBoundaryPollRef.current = null;
          return;
        }
        asyncBoundaryPollRef.current = window.setTimeout(poll, 15_000);
      });
    };
    asyncBoundaryPollRef.current = window.setTimeout(poll, 3_000);
  }

  function syncProposal(proposal: Proposal) {
    if (!proposal.proposal_id || !proposal.status) {
      return;
    }
    queryClient.setQueryData<ProjectSnapshot>(queryKeys.project(projectId), (previous) => {
      if (!previous) return previous;
      const proposals = [...previous.proposals];
      const index = proposals.findIndex((item) => item.proposal_id === proposal.proposal_id);
      if (index >= 0) {
        proposals[index] = proposal;
      } else {
        proposals.push(proposal);
      }
      return { ...previous, proposals };
    });
    queryClient.setQueryData<{ items: Proposal[] }>(queryKeys.advancedProposals(projectId), (previous) => {
      const items = [...(previous?.items ?? [])];
      const index = items.findIndex((item) => item.proposal_id === proposal.proposal_id);
      if (index >= 0) {
        items[index] = proposal;
      } else {
        items.push(proposal);
      }
      return { items };
    });
    schedulePartialRefresh();
  }

  function syncSnapshot(snapshot: ProjectSnapshot) {
    queryClient.setQueryData(queryKeys.project(projectId), snapshot);
    queryClient.setQueryData(queryKeys.advancedProposals(projectId), { items: snapshot.proposals });
  }

  function findToolIndex(tools: ToolUseState[], toolCallId?: string, toolName?: string) {
    if (toolCallId) {
      return tools.findIndex((tool) => tool.id === toolCallId);
    }
    for (let index = tools.length - 1; index >= 0; index -= 1) {
      if (tools[index].toolName === toolName) {
        return index;
      }
    }
    return -1;
  }

  function upsertTool(tools: ToolUseState[], event: Extract<ChatStreamEvent, { type: "tool_start" | "tool_end" }>) {
    const nextTools = [...tools];
    const toolId = event.tool_call_id || `tool_${event.tool_name || "unknown"}`;
    const existingIndex = findToolIndex(nextTools, event.tool_call_id, event.tool_name);
    const nextTool: ToolUseState = {
      id: existingIndex >= 0 ? nextTools[existingIndex].id : toolId,
      toolName: event.tool_name,
      label: event.label || event.done_label || event.tool_name || "Tool",
      status: event.type === "tool_end" ? (event.is_error ? "error" : "done") : "running",
    };
    if (existingIndex >= 0) {
      nextTools[existingIndex] = {
        ...nextTools[existingIndex],
        ...nextTool,
        label: nextTool.label || nextTools[existingIndex].label,
      };
      return nextTools;
    }
    nextTools.push(nextTool);
    return nextTools;
  }

  function applyStreamEvent(messageId: string, event: ChatStreamEvent) {
    updateOrInsertStreamMessage(messageId, (current) => {
      const timeline = current.timeline ?? [];
      switch (event.type) {
        case "thinking_start":
          {
            const itemId = timelineItemId("thinking", event.content_index, `${timeline.length}`, event.assistant_turn_index);
            const existing = timeline.find((item) => item.id === itemId);
            const nextTimeline = timeline.filter((item) => item.id !== "thinking_hb");
            return {
              ...current,
              thinkingState: "running",
              thinking: current.thinking ?? "",
              state: current.content ? "streaming" : "thinking",
              timeline: upsertTimelineItem(
                nextTimeline,
                {
                  id: itemId,
                  kind: "thinking",
                  content: existing?.content ?? "",
                  status: "running",
                  startedAt: existing?.startedAt ?? event.started_at ?? Date.now(),
                  endedAt: undefined,
                },
                (item) => item.id === itemId,
              ),
            };
          }
        case "thinking_delta":
          {
            const itemId =
              event.content_index !== undefined
                ? timelineItemId("thinking", event.content_index, `${timeline.length}`, event.assistant_turn_index)
                : timeline.find((item) => item.kind === "thinking" && item.status === "running")?.id ?? timelineItemId("thinking", undefined, `${timeline.length}`);
            const existingContent = timeline.find((item) => item.id === itemId)?.content ?? "";
            return {
              ...current,
              thinkingState: "running",
              thinking: `${current.thinking || ""}${event.delta || ""}`,
              state: current.content ? "streaming" : "thinking",
              timeline: upsertTimelineItem(
                timeline,
                {
                  id: itemId,
                  kind: "thinking",
                  content: `${existingContent}${event.delta || ""}`,
                  status: "running",
                  startedAt: timeline.find((item) => item.id === itemId)?.startedAt ?? Date.now(),
                  endedAt: undefined,
                },
                (item) => item.id === itemId,
              ),
            };
          }
        case "thinking_end":
          {
            const itemId =
              event.content_index !== undefined
                ? timelineItemId("thinking", event.content_index, `${timeline.length}`, event.assistant_turn_index)
                : timeline.find((item) => item.kind === "thinking" && item.status === "running")?.id ?? timelineItemId("thinking", undefined, `${timeline.length}`);
            const existingContent = timeline.find((item) => item.id === itemId)?.content ?? "";
            const existing = timeline.find((item) => item.id === itemId);
            const content = event.content?.trim() || existingContent || current.thinking || "";
            return {
              ...current,
              thinkingState: content ? "done" : current.thinkingState,
              thinking: content || current.thinking,
              timeline: upsertTimelineItem(
                timeline,
                {
                  id: itemId,
                  kind: "thinking",
                  content,
                  status: "done",
                  startedAt: existing?.startedAt ?? event.started_at ?? Date.now(),
                  endedAt: event.ended_at ?? Date.now(),
                },
                (item) => item.id === itemId,
              ),
            };
          }
        case "heartbeat":
          {
            const hbContent = current.thinking || event.message || "Manager 正在生成回复…";
            const runningThinkingIndex = timeline.findIndex((item) => item.kind === "thinking" && item.status === "running" && item.id !== "thinking_hb");
            if (runningThinkingIndex >= 0) {
              const nextTimeline = [...timeline];
              nextTimeline[runningThinkingIndex] = {
                ...nextTimeline[runningThinkingIndex],
                content: hbContent,
                startedAt: nextTimeline[runningThinkingIndex].startedAt ?? Date.now(),
              };
              return {
                ...current,
                thinkingState: current.state === "done" ? current.thinkingState : "running",
                thinking: hbContent,
                state: current.content ? current.state : "thinking",
                timeline: nextTimeline,
              };
            }
            if (!current.content) {
              const existingHb = timeline.find((item) => item.id === "thinking_hb");
              return {
                ...current,
                thinkingState: current.state === "done" ? current.thinkingState : "running",
                thinking: hbContent,
                state: current.content ? current.state : "thinking",
                timeline: upsertTimelineItem(
                  timeline,
                  {
                    id: "thinking_hb",
                    kind: "thinking",
                    content: hbContent,
                    status: "running",
                    startedAt: existingHb?.startedAt ?? Date.now(),
                    endedAt: undefined,
                  },
                  (item) => item.id === "thinking_hb",
                ),
              };
            }
            return {
              ...current,
              thinkingState: current.state === "done" ? current.thinkingState : "running",
              thinking: hbContent,
              state: current.content ? current.state : "thinking",
            };
          }
        case "text_delta":
          {
            const isCmd = messageId.startsWith("cmd_");
            const itemId =
              event.content_index !== undefined
                ? timelineItemId(isCmd ? "command" : "text", event.content_index, `${timeline.length}`, event.assistant_turn_index)
                : (timeline[timeline.length - 1]?.kind === "text" || timeline[timeline.length - 1]?.kind === "command")
                  ? timeline[timeline.length - 1].id
                  : timelineItemId(isCmd ? "command" : "text", undefined, `${timeline.length}`);
            const existingContent = timeline.find((item) => item.id === itemId)?.content ?? "";
            return {
              ...current,
              state: "streaming",
              content: `${current.content}${event.delta || ""}`,
              timeline: upsertTimelineItem(
                timeline,
                {
                  id: itemId,
                  kind: isCmd ? "command" : "text",
                  content: `${existingContent}${event.delta || ""}`,
                  status: "running",
                },
                (item) => item.id === itemId,
              ),
            };
          }
        case "usage":
          return {
            ...current,
            tokenUsage: normalizeTokenUsage(event.usage) ?? current.tokenUsage,
          };
        case "tool_start":
          {
            const itemId = event.tool_call_id || timelineItemId("tool", undefined, `${event.tool_name || "unknown"}_${timeline.length}`);
            const existing = timeline.find((item) => item.id === itemId);
            const nextTimeline = settleRunningTimelineText(timeline);
            return {
              ...current,
              tools: upsertTool(current.tools || [], event),
              state: "streaming",
              timeline: upsertTimelineItem(
                nextTimeline,
                {
                  id: itemId,
                  kind: "tool",
                  label: event.label || event.done_label || event.tool_name || "Tool",
                  toolName: event.tool_name,
                  status: "running",
                  startedAt: existing?.startedAt ?? Date.now(),
                  endedAt: undefined,
                },
                (item) => item.id === itemId,
              ),
            };
          }
        case "tool_end":
          if (!event.is_error && event.tool_name && PROJECT_MUTATION_TOOLS.test(event.tool_name)) {
            schedulePartialRefresh();
            if (RUN_CONTROL_TOOLS.test(event.tool_name)) {
              scheduleDelayedPartialRefresh();
            }
          }
          {
            const fallbackIndex = lastTimelineIndex(
              timeline,
              (item) => item.kind === "tool" && item.status === "running" && (!event.tool_name || item.toolName === event.tool_name),
            );
            const itemId = event.tool_call_id || (fallbackIndex >= 0 ? timeline[fallbackIndex].id : timelineItemId("tool", undefined, `${event.tool_name || "unknown"}_${timeline.length}`));
            const existing = timeline.find((item) => item.id === itemId);
            return {
              ...current,
              tools: upsertTool(current.tools || [], event),
              timeline: upsertTimelineItem(
                timeline,
                {
                  id: itemId,
                  kind: "tool",
                  label: event.done_label || event.label || event.tool_name || "Tool",
                  toolName: event.tool_name,
                  status: event.is_error ? "error" : "done",
                  startedAt: existing?.startedAt ?? Date.now(),
                  endedAt: Date.now(),
                },
                (item) => item.id === itemId,
              ),
            };
          }
        case "tool_report":
          {
            const reportedRunId = typeof event.details?.run_id === "string" ? event.details.run_id : null;
            if (event.tool_name && RUN_CONTROL_TOOLS.test(event.tool_name)) {
              schedulePartialRefresh(reportedRunId);
              scheduleDelayedPartialRefresh(reportedRunId);
            } else if (reportedRunId) {
              void queryClient.refetchQueries({ queryKey: queryKeys.runEvents(projectId, reportedRunId), type: "active" });
            }
            const fallbackIndex = lastTimelineIndex(
              timeline,
              (item) => item.kind === "tool" && (!event.tool_name || item.toolName === event.tool_name),
            );
            const itemId =
              event.tool_call_id ||
              (fallbackIndex >= 0 ? timeline[fallbackIndex].id : timelineItemId("tool", undefined, `${event.tool_name || "unknown"}_${timeline.length}`));
            const existing = timeline.find((item) => item.id === itemId);
            return {
              ...current,
              timeline: upsertTimelineItem(
                timeline,
                {
                  id: itemId,
                  kind: "tool",
                  label: existing?.label || event.tool_name || "Tool",
                  toolName: event.tool_name,
                  content: event.summary || existing?.content || "",
                  status: existing?.status === "error" ? "error" : "done",
                  startedAt: existing?.startedAt ?? Date.now(),
                  endedAt: existing?.endedAt ?? Date.now(),
                },
                (item) => item.id === itemId,
              ),
              tools: (current.tools ?? []).map((tool) =>
                tool.id === itemId || (!event.tool_call_id && tool.toolName === event.tool_name)
                  ? {
                      ...tool,
                      label: tool.label || event.tool_name || "Tool",
                      status: tool.status === "error" ? "error" : "done",
                    }
                  : tool,
              ),
            };
          }
        case "proposal":
          {
            const proposal = parseProposal(event.proposal);
            if (proposal) {
              syncProposal(proposal);
            }
            return {
              ...current,
              proposal: proposal || current.proposal,
            };
          }
        case "response":
          {
            const proposal = parseProposal(event.response?.proposal);
            if (proposal) {
              syncProposal(proposal);
            }
            if (event.response?.metadata?.async_boundary_tool === "install_runtime_dependencies") {
              scheduleAsyncBoundaryPoll((event.response.metadata.async_boundary_job_id as string) || null);
            }
            return {
              ...current,
              state: "done",
              content: event.response?.message || current.content,
              thinking: event.response?.thinking?.trim() || current.thinking,
              thinkingState: current.thinking || event.response?.thinking ? "done" : current.thinkingState,
              proposal: proposal || current.proposal,
              tokenUsage: normalizeTokenUsage(event.response?.metadata?.token_usage) ?? current.tokenUsage,
              timeline: (() => {
                let nextTimeline = settleCompletedTimelineTools([...timeline]);
                if (event.response?.thinking?.trim()) {
                  const runningThinkingIndex = lastTimelineIndex(nextTimeline, (item) => item.kind === "thinking" && item.status === "running");
                  if (runningThinkingIndex >= 0) {
                    const item = nextTimeline[runningThinkingIndex];
                    nextTimeline = upsertTimelineItem(
                      nextTimeline,
                      {
                        ...item,
                        content: item.content || event.response.thinking.trim(),
                        status: "done",
                        endedAt: item.endedAt ?? Date.now(),
                      },
                      (candidate) => candidate.id === item.id,
                    );
                  } else if (!nextTimeline.some((item) => item.kind === "thinking" && item.content)) {
                    nextTimeline.push({
                      id: "thinking_final",
                      kind: "thinking",
                      content: event.response.thinking.trim(),
                      status: "done",
                      startedAt: Date.now(),
                      endedAt: Date.now(),
                    });
                  }
                }
                if (event.response?.message) {
                  const isCmd = messageId.startsWith("cmd_");
                  const targetKind = isCmd ? "command" : "text";
                  const runningTextIndex = lastTimelineIndex(nextTimeline, (item) => item.kind === targetKind && item.status === "running");
                  if (runningTextIndex >= 0) {
                    const item = nextTimeline[runningTextIndex];
                    nextTimeline = upsertTimelineItem(
                      nextTimeline,
                      {
                        ...item,
                        content: item.content || event.response.message,
                        status: "done",
                      },
                      (candidate) => candidate.id === item.id,
                    );
                  } else if (!nextTimeline.some((item) => item.kind === targetKind && item.content)) {
                    nextTimeline.push({
                      id: "text_final",
                      kind: targetKind,
                      content: event.response.message,
                      status: "done",
                    });
                  }
                }
                return settleCompletedTimelineTools(nextTimeline);
              })(),
              tools: (current.tools ?? []).map((tool) =>
                tool.status === "running"
                  ? {
                      ...tool,
                      status: "done" as const,
                    }
                  : tool,
              ),
            };
          }
        case "done":
          return {
            ...current,
            state: current.state === "error" ? "error" : "done",
            thinkingState: current.thinkingState === "running" ? "done" : current.thinkingState,
            timeline: settleRunningTimelineItems(timeline, "done"),
          };
        case "error": {
          const detail = event.detail || current.content || "请求失败。";
          const isCmd = messageId.startsWith("cmd_");
          return {
            ...current,
            state: "error",
            thinkingState: current.thinkingState === "running" ? "error" : current.thinkingState,
            content: detail,
            timeline: isCmd
              ? upsertTimelineItem(
                  settleRunningTimelineItems(timeline, "error"),
                  {
                    id: `${messageId}_text`,
                    kind: "command",
                    content: detail,
                    status: "error",
                  },
                  (item) => item.id === `${messageId}_text` || item.kind === "command",
                )
              : settleRunningTimelineItems(timeline, "error"),
          };
        }
        default:
          return current;
      }
    });
  }

  function applyCompactStreamEvent(event: ChatStreamEvent) {
    if (event.type === "compact_start") {
      upsertCompactMessage({
        id: event.compact_id,
        kind: "compact",
        content: "",
        status: "running",
        startedAt: Date.now(),
      });
      return true;
    }
    if (event.type === "compact_delta") {
      upsertCompactMessage({
        id: event.compact_id,
        kind: "compact",
        content: event.content || "",
        status: "running",
        startedAt: Date.now(),
      });
      return true;
    }
    if (event.type === "compact_end") {
      finalizeCompaction({
        id: event.compact_id,
        kind: "compact",
        content: event.content || "",
        status: "done",
        startedAt: Date.now() - (event.duration_ms ?? 0),
        endedAt: Date.now(),
        durationMs: event.duration_ms,
        firstKeptMessageId: event.first_kept_message_id,
        tokensBefore: event.tokens_before,
        tokensAfter: event.tokens_after,
        provider: event.provider,
        model: event.model,
      });
      return true;
    }
    if (event.type === "compact_error") {
      upsertCompactMessage({
        id: event.compact_id,
        kind: "compact",
        content: event.message || "上下文压缩失败。",
        status: "error",
        startedAt: Date.now(),
        endedAt: Date.now(),
      });
      return true;
    }
    return false;
  }

  async function runManualCompaction() {
    if (busy || !sessionId) return;
    const startedAt = Date.now();
    const compactId = `compact_manual_${Date.now().toString(36)}`;
    upsertCompactMessage({
      id: compactId,
      kind: "compact",
      content: "正在压缩历史对话，后续回复会使用压缩后的上下文。",
      status: "running",
      startedAt,
    });
    setDraft("");
    setBusy(true);
    setError(null);
    try {
      const response = await api.compactChatSession(projectId, serializeSessionMessages(messages), thinkingEffort, sessionId);
      finalizeCompaction({
        id: compactId,
        kind: "compact",
        content: response.summary,
        status: "done",
        startedAt,
        endedAt: Date.now(),
        durationMs: response.duration_ms,
        firstKeptMessageId: response.first_kept_message_id,
        tokensBefore: response.tokens_before,
        tokensAfter: response.tokens_after,
        provider: response.provider ?? undefined,
        model: response.model ?? undefined,
      });
    } catch (nextError) {
      const message = nextError instanceof Error ? nextError.message : "上下文压缩失败。";
      setError(message);
      upsertCompactMessage({
        id: compactId,
        kind: "compact",
        content: message,
        status: "error",
        startedAt,
        endedAt: Date.now(),
      });
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!draft.trim() || busy || !sessionId) return;
    const text = draft.trim();
    if (text === "/compact") {
      await runManualCompaction();
      return;
    }
    const priorMessages = messages;
    const messageAttachments = attachments.map((attachment) => ({ ...attachment }));
    const context = attachments.length
      ? `上下文附件: ${attachments.map((a) => `[${a.type === "card" ? "卡片" : "资产"}: ${a.label}; id=${a.id}]`).join(" ")}\n\n${text}`
      : text;
    const history = buildChatHistory(priorMessages);
    const chatContext: ChatRequestContext = {
      script_preference: scriptPreference,
      python_runtime: runtimeForChatContext(globalPythonRuntime),
      r_runtime: runtimeForChatContext(globalRRuntime),
    };
    const isSlashCommand = parseSlashCommand(text).isCmd;
    const suffix = createMessageId().replace("msg_", "");
    const userMessageId = isSlashCommand ? `cmd_usr_${suffix}` : createMessageId();
    const managerMessageId = isSlashCommand ? `cmd_mgr_${suffix}` : createMessageId();
    setMessages((prev) => [
      ...prev,
      {
        id: userMessageId,
        role: "user",
        content: text,
        attachments: isSlashCommand ? [] : messageAttachments,
        state: "done",
        timeline: [{ id: `${userMessageId}_text`, kind: isSlashCommand ? "command" : "text", content: text, status: "done" }],
      },
      ...(isAutoOwnerSession && effectiveManagerAuto?.enabled && !isSlashCommand
        ? []
        : [{ id: managerMessageId, role: "manager" as const, content: "", thinking: "", thinkingState: "idle" as const, tools: [], state: "thinking" as const, timeline: [] }]),
    ]);
    setDraft("");
    if (messageAttachments.length) {
      clearAttachments(projectId);
    }
    setBusy(true);
    setError(null);
    stopRequestedRef.current = false;
    if (isAutoOwnerSession && effectiveManagerAuto?.enabled && !isSlashCommand) {
      try {
        const response = await api.addManagerAutoDirective(projectId, sessionId, text, userMessageId);
        applyManagerAutoState(response.state);
        clearAttachments(projectId);
        const ack = response.wake_event ? "已收到追加指令，AUTO 正在处理。" : "已加入 auto 指令队列，将在下一次唤醒时处理。";
        setMessages((prev) => [
          ...prev,
          {
            id: managerMessageId,
            role: "manager",
            content: ack,
            state: "done",
            timeline: [{ id: `${managerMessageId}_text`, kind: "text", content: ack, status: "done" }],
          },
        ]);
        setDraft("");
        await onRefresh();
      } catch (nextError) {
        setError(nextError instanceof Error ? nextError.message : "追加 auto 指令失败。");
      } finally {
        setBusy(false);
      }
      return;
    }
    const abortController = new AbortController();
    activeStreamControllerRef.current = abortController;
    try {
      let streamError: string | null = null;
      await api.streamChat(
        projectId,
        isSlashCommand ? text : context,
        thinkingEffort,
        history,
        serializeSessionMessages(priorMessages),
        (event) => {
          if (applyCompactStreamEvent(event)) {
            return;
          }
          applyStreamEvent(managerMessageId, event);
          if (event.type === "error") {
            streamError = event.detail || "Chat failed.";
          }
        },
        abortController.signal,
        chatContext,
        sessionId,
        userMessageId,
      );
      if (streamError) {
        throw new Error(streamError);
      }
      clearAttachments(projectId);
      await onRefresh();
    } catch (nextError) {
      if (stopRequestedRef.current) {
        setError(null);
        updateMessage(managerMessageId, (current) => ({
          ...current,
          state: "done",
          tools: settleInterruptedTools(current.tools),
          thinkingState: current.thinkingState === "running" ? "done" : current.thinkingState,
          content: current.content || "已停止本次生成。",
          thinking: current.thinking,
          timeline: settleRunningTimelineItems(current.timeline ?? [], "done"),
        }));
        return;
      }
      if (isAbortLikeError(nextError)) {
        setError(null);
        setMessages((previous) =>
          previous
            .map((message) =>
              message.id === managerMessageId && !isEmptyPendingManagerMessage(message)
                ? {
                    ...message,
                    state: message.state === "error" ? ("error" as const) : ("done" as const),
                    thinkingState: message.thinkingState === "running" ? "done" : message.thinkingState,
                    timeline: settleRunningTimelineItems(message.timeline ?? [], "done"),
                  }
                : message,
            )
            .filter((message) => message.id !== managerMessageId || !isEmptyPendingManagerMessage(message)),
        );
        return;
      }
      const message = nextError instanceof Error ? nextError.message : "Chat failed.";
      setError(message);
      const isCmd = managerMessageId.startsWith("cmd_");
      updateMessage(managerMessageId, (current) => {
        const detail = current.content || message || "请求失败。";
        return {
          ...current,
          state: "error",
          thinkingState: current.thinkingState === "running" ? "error" : current.thinkingState,
          content: detail,
          thinking: current.thinking,
          timeline: isCmd
            ? upsertTimelineItem(
                settleRunningTimelineItems(current.timeline ?? [], "error"),
                {
                  id: `${managerMessageId}_text`,
                  kind: "command",
                  content: detail,
                  status: "error",
                },
                (item) => item.id === `${managerMessageId}_text` || item.kind === "command",
              )
            : settleRunningTimelineItems(current.timeline ?? [], "error"),
        };
      });
    } finally {
      activeStreamControllerRef.current = null;
      stopRequestedRef.current = false;
      setBusy(false);
    }
  }

  function stopGeneration() {
    if (!busy || !activeStreamControllerRef.current) {
      return;
    }
    stopRequestedRef.current = true;
    activeStreamControllerRef.current.abort(new Error("user_aborted"));
  }

  async function stopAutoFromComposer() {
    if (!sessionId || autoStopPending) return;
    setAutoStopPending(true);
    setError(null);
    try {
      const response = await api.stopManagerAuto(projectId, sessionId, "user_stop", "因用户停止任务，已退出 auto 模式。");
      applyManagerAutoState(response.state);
      await onRefresh();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "停止 Auto 推进失败。");
    } finally {
      setAutoStopPending(false);
    }
  }



  function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    uploadMutation.mutate(file);
  }

  async function accept(proposalId: string) {
    setBusy(true);
    setError(null);
    try {
      const response = (await acceptProposalMutation.mutateAsync(proposalId)) as ProposalMutationResponse;
      if (response.snapshot) {
        syncSnapshot(response.snapshot);
      } else if (response.proposal) {
        syncProposal(response.proposal);
      }
      if (response.proposal) {
        setMessages((prev) =>
          prev.map((m) =>
            m.proposal?.proposal_id === proposalId ? { ...m, proposal: response.proposal! } : m,
          ),
        );
      }
      await onRefresh();
    } catch (nextError) {
      const message = nextError instanceof Error ? nextError.message : "Accept failed.";
      if (message.includes("cannot accept")) {
        setError(`该 proposal 当前已不是 proposed 状态：${message}`);
      } else if (message.includes("409")) {
        setError(`提案应用时发生冲突：${message}`);
      } else {
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  }

  async function reject(proposalId: string) {
    setBusy(true);
    setError(null);
    try {
      const response = (await rejectProposalMutation.mutateAsync(proposalId)) as ProposalMutationResponse;
      if (response.proposal) {
        syncProposal(response.proposal);
      }
      if (response.proposal) {
        setMessages((prev) =>
          prev.map((m) =>
            m.proposal?.proposal_id === proposalId ? { ...m, proposal: response.proposal! } : m,
          ),
        );
      }
      await onRefresh();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Reject failed.");
    } finally {
      setBusy(false);
    }
  }

  async function modify(proposalId: string) {
    if (!editDraft.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const response = await modifyProposalMutation.mutateAsync({
        proposalId,
        message: editDraft.trim(),
      });
      const proposal = response.proposal;
      syncProposal(proposal);
      setMessages((prev) => [
        ...prev,
        { id: createMessageId(), role: "user", content: `修改提案：${editDraft.trim()}`, state: "done" },
        { id: createMessageId(), role: "manager", content: proposal.summary, proposal, state: "done" },
      ]);
      setEditingProposalId(null);
      setEditDraft("");
      await onRefresh();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Modify failed.");
    } finally {
      setBusy(false);
    }
  }

  const openProposals = useMemo(() => {
    const messageProposalIds = new Set(messages.filter((m) => m.proposal).map((m) => m.proposal!.proposal_id));
    return proposals.filter((p) => p.status === "proposed" && !messageProposalIds.has(p.proposal_id));
  }, [proposals, messages]);
  const contextWindow = useMemo(() => {
    const draftTokens = estimateTokens(draft);
    const attachmentTokens = attachments.reduce((total, item) => total + estimateTokens(`${item.type}:${item.label}:${item.id}`), 0);
    const latestUsage = [...messages].reverse().find((message) => message.role === "manager" && message.tokenUsage)?.tokenUsage;
    const contextLimit = latestUsage?.context_window_tokens || MANAGER_CONTEXT_WINDOW_TOKENS;
    const trailingTokens = draftTokens + attachmentTokens;
    const historyTokens = latestUsage
      ? latestUsage.total_tokens + trailingTokens
      : messages.reduce((total, message) => {
          const attachmentText = (message.attachments ?? []).map((item) => item.label).join(" ");
          return total + estimateTokens(`${message.role}: ${toHistoryContent(message)}\n${attachmentText}`);
        }, 0) + trailingTokens;
    const ratio = Math.min(1, historyTokens / contextLimit);
    const fillPercent = Math.max(0.6, ratio * 100);
    const remainingTokens = Math.max(0, contextLimit - historyTokens);
    const level = ratio >= 0.82 ? "high" : ratio >= 0.58 ? "medium" : "low";
    const sourceLabel = latestUsage ? "DeepSeek 实际 usage + 当前输入估算" : "字符粗略估算";
    return {
      estimatedTokens: historyTokens,
      fillPercent,
      level,
      title: `DeepSeek context window: ${sourceLabel}，当前约 ${historyTokens.toLocaleString()} tokens，剩余约 ${remainingTokens.toLocaleString()} tokens。上下文窗口 ${contextLimit.toLocaleString()} tokens。`,
    };
  }, [attachments, draft, messages]);
  const displayMessages = messages.length ? messages : [DEFAULT_MANAGER_MESSAGE];
  const sessionLoadError = chatSessionQuery.error instanceof Error ? chatSessionQuery.error.message : null;
  const sessionBusy = !sessionId || chatSessionQuery.isLoading;
  const composerInputDisabled = sessionBusy || Boolean(sessionLoadError) || autoScopedActive;
  const composerSendDisabled =
    autoComposerState === "auto_running"
      ? autoStopPending || !sessionId || sessionBusy || Boolean(sessionLoadError)
      : !draft.trim() || sessionBusy || Boolean(sessionLoadError);
  const composerButtonTitle =
    autoComposerState === "auto_running"
      ? "停止当前后台继续执行"
      : "发送";
  const composerButtonClass =
    autoComposerState === "auto_running"
      ? "auto-running"
      : "";

  function renderStatusIcon(status: ToolState | ChatMessage["thinkingState"]) {
    if (status === "running") {
      return <Loader2 size={12} className="spinning" />;
    }
    if (status === "done") {
      return <Check size={12} />;
    }
    if (status === "error") {
      return <AlertTriangle size={12} />;
    }
    return <Sparkles size={12} />;
  }

  function effortLabel(value: ThinkingEffort) {
    if (value === "low") return "Low";
    if (value === "high") return "High";
    return "Medium";
  }

  function renderMessageText(message: ChatMessage) {
    if (message.role === "manager") {
      return (
        <div className="manager-markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          {message.state === "thinking" || message.state === "streaming" ? <span className="manager-stream-cursor" /> : null}
        </div>
      );
    }
    return (
      <>
        {message.content}
        {message.state === "thinking" || message.state === "streaming" ? <span className="manager-stream-cursor" /> : null}
      </>
    );
  }

  function renderTimelineItem(message: ChatMessage, item: MessageTimelineItem) {
    if (item.kind === "thinking" || item.kind === "compact") {
      const elapsed = formatElapsedTime(item.startedAt, item.endedAt);
      const running = item.status === "running";
      const prefix = item.kind === "compact" ? (running ? "正在压缩上下文" : "已压缩上下文") : running ? "思考中" : "已思考";
      const label = `${prefix}${elapsed ? ` ${elapsed}` : ""}`;
      return (
        <details key={item.id} className={`manager-thinking-panel ${running ? "running" : "done"}`} open={running}>
          <summary>
            <span className="manager-thinking-label">
              {label}
              <span className="manager-thinking-arrow">{running ? "↓" : "<"}</span>
            </span>
            <span className="manager-thinking-line" />
          </summary>
          <div
            ref={(element) => {
              thinkingRefs.current[item.id] = element;
            }}
            className="manager-thinking-text"
          >
            {(item.content || "").split("\n").map((line, index) => (
              <div key={`${item.id}-${index}`}>{line}</div>
            ))}
            {item.kind === "compact" && (item.tokensBefore || item.tokensAfter) ? (
              <div className="meta-text">
                {item.tokensBefore ? `压缩前 ${Math.round(item.tokensBefore).toLocaleString()}` : null}
                {item.tokensBefore && item.tokensAfter ? " · " : null}
                {item.tokensAfter ? `压缩后 ${Math.round(item.tokensAfter).toLocaleString()}` : null}
              </div>
            ) : null}
          </div>
        </details>
      );
    }
    if (item.kind === "tool") {
      const label = item.label || (item.status === "running" ? "正在执行工具" : "已完成工具调用");
      return (
        <div key={item.id} className={`manager-tool-divider ${item.status ?? "done"}`}>
          <div className="manager-tool-divider-main">
            <span className="manager-tool-divider-label">{label}</span>
            <span className="manager-tool-divider-line" />
          </div>
          {item.content ? <div className="manager-tool-report">{item.content}</div> : null}
        </div>
      );
    }
    return (
      <div key={item.id} className={`manager-message-bubble ${message.role}`}>
        <div className="manager-message-text">
          {renderMessageText({
            ...message,
            content: item.content || "",
            state: item.status === "running" ? message.state : "done",
          })}
        </div>
        {message.role === "manager" && item.kind === "text" && message.proposal ? renderProposalControls(message.proposal) : null}
      </div>
    );
  }

  function renderMessageAttachments(message: ChatMessage) {
    if (!message.attachments?.length) {
      return null;
    }
    return (
      <div className={`manager-message-attachments ${message.role}`}>
        {message.attachments.map((attachment) => (
          <a
            key={`${message.id}-${attachment.id}`}
            className="manager-message-attachment"
            href={attachment.type === "asset" ? api.getResultAssetContentUrl(projectId, attachment.id) : undefined}
            onClick={(event) => {
              if (attachment.type !== "asset") {
                event.preventDefault();
              }
            }}
            target={attachment.type === "asset" ? "_blank" : undefined}
            rel={attachment.type === "asset" ? "noreferrer" : undefined}
            title={attachment.label}
          >
            {attachment.type === "card" ? <Sparkles size={13} /> : <FileText size={13} />}
            <span>{attachment.label}</span>
          </a>
        ))}
      </div>
    );
  }

  return (
    <section className="manager-chat-panel" style={{ maxHeight: "calc(100vh - 140px)" }}>
      <div className="manager-chat-body">
        {error ? (
          <div className="notice-panel error notice-toast manager-chat-toast">
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <AlertTriangle size={14} />
              {error}
            </div>
          </div>
        ) : null}

        <div ref={scrollRef} className="manager-chat-scroll">
          {sessionBusy ? (
            <div className="manager-session-empty">正在加载当前 session…</div>
          ) : sessionLoadError ? (
            <div className="manager-session-empty">当前 session 加载失败：{sessionLoadError}</div>
          ) : (
            displayMessages.map((message) => (
              <div key={message.id} className={`manager-message-row ${message.role}`}>
                <div className={`manager-message-content ${message.role}`}>
                {message.role === "user" ? renderMessageAttachments(message) : null}
                  <div className="manager-message-stack">
                    {(() => {
                      if (message.timeline?.length) {
                        return message.timeline.map((item) => renderTimelineItem(message, item));
                      }
                      if (message.thinkingState === "running" && message.thinking) {
                        return renderTimelineItem(message, {
                          id: `${message.id}_thinking_fallback`,
                          kind: "thinking",
                          content: message.thinking,
                          status: "running",
                        });
                      }
                      return renderTimelineItem(message, {
                        id: `${message.id}_fallback`,
                        kind: "text" as const,
                        content: message.content,
                        status: "done" as const,
                      });
                    })()}
                  </div>
                {message.role === "manager" ? renderMessageAttachments(message) : null}
                </div>
              </div>
            ))
          )}
        </div>

        {attachments.length ? (
          <div className="attachment-bar">
            {attachments.map((a) => (
              <span key={a.id} className="attachment-pill" onClick={() => removeAttachment(projectId, a.id)}>
                {a.type === "card" ? "📋" : "📎"} <span className="label">{a.label}</span>
                <X size={12} />
              </span>
            ))}
          </div>
        ) : null}

        <div className="manager-composer-shell">
          {!effectiveManagerAuto?.enabled && effectiveManagerAuto?.stop_reason === "auto_chain_budget_exceeded" ? (
            <div className="manager-auto-budget-notice">
              <AlertTriangle size={14} />
              <span>
                {effectiveManagerAuto?.stop_message || "AUTO 已暂停：连续唤醒达到安全上限。请重新发送 /auto <目标> 继续。"}
              </span>
            </div>
          ) : null}
          {slashCommandState && slashCommandOptions.length ? (
            <div className="manager-slash-hint">
              {slashCommandOptions.map((option, index) => (
                <button
                  key={option.command}
                  type="button"
                  className={index === slashCommandIndex ? "active" : ""}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    insertSlashCommand(option);
                  }}
                >
                  <span className="manager-slash-command">{option.command}</span>
                  <span>{option.label}</span>
                </button>
              ))}
            </div>
          ) : null}
          {uploadProgress ? (
            <div className="manager-upload-progress" role="status" aria-live="polite">
              <div className="manager-upload-progress-header">
                <span className="manager-upload-progress-name">{uploadProgress.fileName}</span>
                <span className="manager-upload-progress-controls">
                  <span className="manager-upload-progress-meta">
                    {uploadProgress.status === "processing"
                      ? "处理中"
                      : uploadProgress.percent == null
                        ? `上传中 · ${formatUploadSpeed(uploadProgress.speedBytesPerSecond)}`
                        : `${uploadProgress.percent}% · ${formatUploadSpeed(uploadProgress.speedBytesPerSecond)}`}
                  </span>
                  {uploadProgress.status === "uploading" ? (
                    <button
                      type="button"
                      className="manager-upload-progress-cancel"
                      onClick={() => uploadAbortControllerRef.current?.abort()}
                      aria-label="取消上传"
                    >
                      ✕
                    </button>
                  ) : null}
                </span>
              </div>
              <div className="manager-upload-progress-track" aria-hidden="true">
                <span
                  className={`manager-upload-progress-fill ${uploadProgress.percent == null ? "indeterminate" : ""}`}
                  style={uploadProgress.percent == null ? undefined : { width: `${uploadProgress.percent}%` }}
                />
              </div>
              <div className="manager-upload-progress-footer">
                <span>{formatUploadBytes(uploadProgress.loaded)}</span>
                <span>{uploadProgress.total ? formatUploadBytes(uploadProgress.total) : formatUploadBytes(uploadProgress.fileSize)}</span>
              </div>
            </div>
          ) : null}
          <div className="manager-composer">
            <input ref={fileInputRef} type="file" hidden onChange={handleFileChange} />
            <button
              className={`manager-upload-button ${uploadMutation.isPending ? "loading" : ""}`}
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={busy || uploadMutation.isPending || composerInputDisabled}
              title="上传文件到后端"
            >
              {uploadMutation.isPending ? <Loader2 size={17} /> : <Paperclip size={17} />}
            </button>
            <textarea
              ref={textareaRef}
              rows={1}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                syncComposerState(e.target.value, e.target.selectionStart);
              }}
              placeholder="shift + enter 快捷发送，/ 发送特殊指令"
              onKeyDown={(e) => {
                if (slashCommandState && slashCommandOptions.length) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setSlashCommandIndex((current) => (current + 1) % slashCommandOptions.length);
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setSlashCommandIndex((current) => (current - 1 + slashCommandOptions.length) % slashCommandOptions.length);
                    return;
                  }
                  if (e.key === "Enter" || e.key === "Tab") {
                    e.preventDefault();
                    insertSlashCommand(slashCommandOptions[slashCommandIndex] ?? slashCommandOptions[0]);
                    return;
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setSlashCommandState(null);
                    return;
                  }
                }
                if (mentionState && mentionOptions.length) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setMentionIndex((current) => (current + 1) % mentionOptions.length);
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setMentionIndex((current) => (current - 1 + mentionOptions.length) % mentionOptions.length);
                    return;
                  }
                  if (e.key === "Enter" || e.key === "Tab") {
                    e.preventDefault();
                    insertMention(mentionOptions[mentionIndex] ?? mentionOptions[0]);
                    return;
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setMentionState(null);
                    return;
                  }
                }
                  if (e.key === "Enter" && (e.shiftKey || e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    if (autoScopedActive) {
                      return;
                    }
                  submit();
                }
              }}
              disabled={composerInputDisabled}
              onClick={(e) => syncComposerState(e.currentTarget.value, e.currentTarget.selectionStart)}
              onKeyUp={(e) => syncComposerState(e.currentTarget.value, e.currentTarget.selectionStart)}
            />
            <div className="manager-composer-actions">
              <div className="manager-effort-menu" ref={effortMenuRef}>
                <button
                  className={`manager-effort-button ${effortMenuOpen ? "open" : ""}`}
                  type="button"
                  onClick={() => setEffortMenuOpen((current) => !current)}
                  disabled={busy || sessionBusy || autoScopedActive}
                  title="Thinking effort"
                >
                  <span>{effortLabel(thinkingEffort)}</span>
                  <ChevronDown size={12} />
                </button>
                {effortMenuOpen ? (
                  <div className="manager-effort-dropdown">
                    {([
                      ["low", "Low"],
                      ["medium", "Medium"],
                      ["high", "High"],
                    ] as const).map(([value, label]) => (
                      <button
                        key={value}
                        type="button"
                        className={`manager-effort-option ${thinkingEffort === value ? "active" : ""}`}
                        onClick={() => {
                          setThinkingEffort(value);
                          setEffortMenuOpen(false);
                        }}
                      >
                        <span>{label}</span>
                        {thinkingEffort === value ? <Check size={12} /> : null}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
              <div
                className={`manager-context-ring ${contextWindow.level}`}
                style={{ "--context-fill": `${contextWindow.fillPercent}%` } as CSSProperties}
                title={contextWindow.title}
                aria-label={contextWindow.title}
              >
                {busy ? (
                  <button className="manager-stop-button" onClick={stopGeneration} type="button" title="停止生成">
                    <Square size={14} />
                  </button>
                ) : (
                  <button
                    className={`manager-send-button ${composerButtonClass}`}
                    onClick={autoComposerState === "auto_running" ? stopAutoFromComposer : submit}
                    disabled={composerSendDisabled}
                    type="button"
                    title={composerButtonTitle}
                  >
                    {autoComposerState === "auto_running" ? (
                      autoStopPending ? <Loader2 size={16} className="spinning" /> : <Square size={14} />
                    ) : (
                      <Send size={16} />
                    )}
                  </button>
                )}
              </div>
            </div>
          </div>
          {mentionState && mentionOptions.length ? (
            <div className="mention-menu">
              {mentionOptions.map((asset, index) => (
                <button
                  key={asset.asset_id}
                  type="button"
                  className={`mention-menu-item ${index === mentionIndex ? "active" : ""}`}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    insertMention(asset);
                  }}
                >
                  <span className="mention-menu-title">@{asset.title}</span>
                  <span className="mention-menu-meta">{asset.asset_type} · {asset.asset_id}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );

  function renderProposalControls(proposal: Proposal) {
    const editing = editingProposalId === proposal.proposal_id;
    if (proposal.status !== "proposed") {
      return (
        <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>
          提案已
          {proposal.status === "accepted"
            ? "接受"
            : proposal.status === "rejected"
              ? "拒绝"
              : `更新为 ${proposal.status}`}
        </div>
      );
    }
    return (
      <div className="proposal-card" style={{ marginTop: 10 }}>
        <h4>📋 {proposal.title}</h4>
        <div className="impact">{proposal.impact_summary}</div>
        {proposal.consistency_warnings.length ? (
          <div className="risk" style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <AlertTriangle size={12} />
            {proposal.consistency_warnings.join(" ")}
          </div>
        ) : null}
        <div className="proposal-actions">
          <button className="btn success" onClick={() => accept(proposal.proposal_id)} disabled={busy}>
            <Check size={14} />
            接受
          </button>
          <button
            className="btn secondary"
            onClick={() => {
              setEditingProposalId(proposal.proposal_id);
              setEditDraft(proposal.summary);
            }}
            disabled={busy}
          >
            <Pencil size={14} />
            修改
          </button>
          <button className="btn danger" onClick={() => reject(proposal.proposal_id)} disabled={busy}>
            <X size={14} />
            拒绝
          </button>
        </div>
        {editing ? (
          <div className="chat-input" style={{ marginTop: 10 }}>
            <textarea
              value={editDraft}
              onChange={(e) => setEditDraft(e.target.value)}
              placeholder="描述你希望如何修改提案"
              style={{ minHeight: 70 }}
            />
            <div className="proposal-actions">
              <button className="btn primary" onClick={() => modify(proposal.proposal_id)} disabled={busy}>
                <Check size={14} />
                提交修改
              </button>
              <button
                className="btn secondary"
                onClick={() => {
                  setEditingProposalId(null);
                  setEditDraft("");
                }}
                disabled={busy}
              >
                <X size={14} />
                取消
              </button>
            </div>
          </div>
        ) : null}
      </div>
    );
  }
}

function getMentionState(text: string, cursor: number): MentionState | null {
  const beforeCursor = text.slice(0, cursor);
  const atIndex = beforeCursor.lastIndexOf("@");
  if (atIndex < 0) {
    return null;
  }
  const prefix = beforeCursor.slice(0, atIndex);
  if (prefix && !/\s$/.test(prefix)) {
    return null;
  }
  const query = beforeCursor.slice(atIndex + 1);
  if (/\s/.test(query)) {
    return null;
  }
  return {
    query,
    start: atIndex,
    end: cursor,
  };
}

function getSlashCommandState(text: string, cursor: number): SlashCommandState | null {
  const beforeCursor = text.slice(0, cursor);
  const match = beforeCursor.match(/^\s*[\/\\]([^\s]*)$/);
  if (!match) {
    return null;
  }
  const prefixLength = beforeCursor.length - match[0].length;
  return {
    query: match[1] ?? "",
    start: prefixLength,
    end: cursor,
  };
}
