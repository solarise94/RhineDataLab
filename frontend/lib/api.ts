import {
  Asset,
  AssetDetail,
  AssetFlow,
  AppSettings,
  ChatSessionDetail,
  ChatSessionMessageRecord,
  ChatSessionSummary,
  ChatUploadResponse,
  DataDirectoryMount,
  ExportHistoryEntry,
  ManagerAutoState,
  CreateProjectPayload,
  WorkspaceRoot,
  WorkspaceEntriesResponse,
  Proposal,
  ProjectFiles,
  ProjectEnvironment,
  ProjectSnapshot,
  ProjectState,
  ProjectSummary,
  ProjectWorkEntriesResponse,
  ProjectRuntimePreferences,
  ReportExportResponse,
  DiagnosticExportResponse,
  ReportSection,
  LibraryEntry,
  LibraryDetailResponse,
  LibraryListResponse,
  RunEvent,
  StartRunResponse,
  TestApiProviderPayload,
  TestApiProviderResponse,
  ExecutorProfile,
  ExecutorProfileListResponse,
  ExecutorProfileValidation,
  UpdateAppSettingsPayload,
  UpdateProjectRuntimePreferencesPayload,
  RuntimeApprovalDecision,
  WorkOrder,
  RuntimeDependencyResolverPlan,
  CardLibraryListResponse,
  CardLibrarySearchResponse,
  CardBlueprintResponse,
  CardBlueprint,
  SaveToLibraryResponse,
  InstantiateBlueprintResponse,
  InstantiateBlueprintRequest,
  ProjectDraftListResponse,
  CreateProjectDraftResponse,
  ProjectDraftResponse,
  PublishDraftResponse,
  BlueprintReviewResult,
  DraftStatus,
  UpdateProjectDraftRequest,
} from "./types";
import type { ChatTokenUsage } from "./types";

export type { ChatTokenUsage } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api";
const UPLOAD_API_BASE = process.env.NEXT_PUBLIC_UPLOAD_API_BASE_URL ?? API_BASE;

export function apiUrl(path: string) {
  return `${API_BASE}${path}`;
}

function uploadUrl(path: string) {
  return `${UPLOAD_API_BASE}${path}`;
}

export interface ChatHistoryMessage {
  role: "user" | "manager";
  content: string;
}

export interface ChatRequestContext {
  selected_card_id?: string | null;
  selected_result_id?: string | null;
  script_preference?: "auto" | "prefer_python" | "prefer_r" | "prefer_mixed";
  python_runtime?: string | null;
  r_runtime?: string | null;
}

export interface UploadProgressEvent {
  loaded: number;
  total?: number;
  lengthComputable: boolean;
}

export type ChatStreamEvent =
  | { type: "thinking_start"; content_index?: number; assistant_turn_index?: number; started_at?: number }
  | { type: "thinking_delta"; delta?: string; content_index?: number; assistant_turn_index?: number }
  | { type: "thinking_end"; content?: string; content_index?: number; assistant_turn_index?: number; started_at?: number; ended_at?: number }
  | { type: "compact_start"; compact_id: string; auto?: boolean }
  | { type: "compact_delta"; compact_id: string; content?: string }
  | {
      type: "compact_end";
      compact_id: string;
      content?: string;
      duration_ms?: number;
      tokens_before?: number;
      tokens_after?: number;
      first_kept_message_id?: string;
      provider?: string;
      model?: string;
      auto?: boolean;
    }
  | { type: "compact_error"; compact_id: string; message?: string; auto?: boolean }
  | { type: "heartbeat"; stage?: string; message?: string }
  | { type: "text_delta"; delta?: string; content_index?: number; assistant_turn_index?: number }
  | { type: "usage"; usage?: ChatTokenUsage }
  | {
      type: "tool_start";
      tool_name?: string;
      tool_call_id?: string;
      label?: string;
      done_label?: string;
    }
  | {
      type: "tool_end";
      tool_name?: string;
      tool_call_id?: string;
      label?: string;
      done_label?: string;
      is_error?: boolean;
    }
  | {
      type: "tool_report";
      tool_name?: string;
      tool_call_id?: string;
      summary?: string;
      details?: Record<string, unknown>;
    }
  | { type: "proposal"; proposal?: unknown }
  | {
      type: "response";
      response?: {
        message: string;
        thinking?: string;
        proposal?: unknown;
        actions: Array<{ label: string; action: string }>;
        warnings: string[];
        metadata?: {
          token_usage?: ChatTokenUsage;
          [key: string]: unknown;
        };
      };
    }
  | { type: "done" }
  | { type: "error"; detail?: string };

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!response.ok) {
    const text = await response.text();
    let parsedDetail: unknown = text;
    let detail = text;
    try {
      const payload = text ? (JSON.parse(text) as { detail?: unknown }) : null;
      parsedDetail = payload?.detail;
      if (typeof payload?.detail === "string" && payload.detail) {
        detail = payload.detail;
      } else if (payload?.detail && typeof payload.detail === "object") {
        const message = (payload.detail as { message?: string }).message;
        detail = message || JSON.stringify(payload.detail);
      }
    } catch {}
    throw new ApiError(response.status, detail || `API error: ${response.status}`, parsedDetail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listProjects() {
    return request<{ items: ProjectSummary[] }>("/projects");
  },
  getAppSettings() {
    return request<AppSettings>("/app-settings");
  },
  updateAppSettings(payload: UpdateAppSettingsPayload) {
    return request<AppSettings>("/app-settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  testApiProvider(payload: TestApiProviderPayload) {
    return request<TestApiProviderResponse>("/app-settings/test-provider", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  listExecutorProfiles() {
    return request<ExecutorProfileListResponse>("/executor-profiles");
  },
  validateExecutorProfile(profile: Partial<ExecutorProfile>) {
    return request<ExecutorProfileValidation>("/executor-profiles/validate", {
      method: "POST",
      body: JSON.stringify(profile),
    });
  },
  saveExecutorProfile(profile: ExecutorProfile) {
    return request<{ profile: ExecutorProfile; validation: ExecutorProfileValidation }>(
      `/executor-profiles/${encodeURIComponent(profile.profile_id)}`,
      {
        method: "PUT",
        body: JSON.stringify(profile),
      },
    );
  },
  deleteExecutorProfile(profileId: string) {
    return request<{ profile_id: string; deleted: boolean }>(
      `/executor-profiles/${encodeURIComponent(profileId)}`,
      { method: "DELETE" },
    );
  },
  createProject(payload: CreateProjectPayload) {
    return request<{ project: ProjectState }>("/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  deleteProject(projectId: string, deleteDirectory?: boolean) {
    const query = deleteDirectory ? "?delete_directory=true" : "";
    return request<{ ok: boolean }>(`/projects/${projectId}${query}`, {
      method: "DELETE",
    });
  },
  listWorkspaceRoots() {
    return request<{ items: WorkspaceRoot[] }>("/workspace-roots");
  },
  listWorkspaceEntries(rootId: string, path: string, kind: "directory" | "all" = "directory") {
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    params.set("kind", kind);
    return request<WorkspaceEntriesResponse>(`/workspace-roots/${encodeURIComponent(rootId)}/entries?${params.toString()}`);
  },
  getProjectDataDirectory(projectId: string) {
    return request<{ data_directory: DataDirectoryMount | null; available: boolean | null }>(`/projects/${encodeURIComponent(projectId)}/data-directory`);
  },
  updateProjectDataDirectory(projectId: string, payload: { root_id: string; path: string }) {
    return request<{ data_directory: DataDirectoryMount }>(`/projects/${encodeURIComponent(projectId)}/data-directory`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  listProjectDataDirectoryEntries(projectId: string, path: string = "", kind: "directory" | "all" = "all") {
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    params.set("kind", kind);
    return request<ProjectWorkEntriesResponse>(`/projects/${encodeURIComponent(projectId)}/data-directory/entries?${params.toString()}`);
  },
  registerDataDirectoryAsset(projectId: string, payload: { path: string }) {
    return request<{ asset: Asset }>(`/projects/${encodeURIComponent(projectId)}/data-directory/assets/register`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  deleteProjectDataDirectory(projectId: string) {
    return request<{ data_directory: DataDirectoryMount | null; detached: boolean }>(
      `/projects/${encodeURIComponent(projectId)}/data-directory`,
      { method: "DELETE" },
    );
  },
  exportAssetToDataDirectory(projectId: string, assetId: string, payload: { destination_path: string; overwrite?: boolean }) {
    return request<{ ok: boolean; asset_id: string; source_path: string; destination_path: string; exported_at: string }>(
      `/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/export-to-data-directory`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },
  getProjectDataDirectoryExportHistory(projectId: string) {
    return request<{ items: ExportHistoryEntry[] }>(`/projects/${encodeURIComponent(projectId)}/data-directory/export-history`);
  },
  getProject(projectId: string) {
    return request<ProjectSnapshot>(`/projects/${projectId}`);
  },
  getProjectEnvironment(projectId: string) {
    return request<ProjectEnvironment>(`/projects/${projectId}/environment`);
  },
  getProjectRuntimePreferences(projectId: string) {
    return request<{ runtime_preferences: ProjectRuntimePreferences }>(`/projects/${projectId}/runtime-preferences`);
  },
  updateProjectRuntimePreferences(projectId: string, payload: UpdateProjectRuntimePreferencesPayload) {
    return request<{ runtime_preferences: ProjectRuntimePreferences }>(`/projects/${projectId}/runtime-preferences`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  getAssetFlow(projectId: string) {
    return request<AssetFlow>(`/projects/${projectId}/asset-flow`);
  },
  getWorkOrder(projectId: string) {
    return request<WorkOrder>(`/projects/${projectId}/work-order`);
  },
  getChatSessions(projectId: string) {
    return request<{ items: ChatSessionSummary[] }>(`/projects/${projectId}/chat-sessions`);
  },
  createChatSession(projectId: string, summary?: string) {
    return request<{ session: ChatSessionDetail }>(`/projects/${projectId}/chat-sessions`, {
      method: "POST",
      body: JSON.stringify({ summary: summary ?? null }),
    });
  },
  getChatSession(projectId: string, sessionId: string) {
    return request<{ session: ChatSessionDetail }>(`/projects/${projectId}/chat-sessions/${sessionId}`);
  },
  saveChatSession(
    projectId: string,
    sessionId: string,
    messages: ChatSessionMessageRecord[],
    summary?: string,
    baseRevision?: number,
  ) {
    return request<{ session: ChatSessionDetail }>(`/projects/${projectId}/chat-sessions/${sessionId}`, {
      method: "PUT",
      body: JSON.stringify({ messages, summary: summary ?? null, base_revision: baseRevision ?? null }),
    });
  },
  appendChatSessionMessages(
    projectId: string,
    sessionId: string,
    messages: ChatSessionMessageRecord[],
    dedupeIds: string[] = [],
  ) {
    return request<{ session: ChatSessionDetail }>(`/projects/${projectId}/chat-sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ messages, dedupe_ids: dedupeIds }),
    });
  },
  deleteChatSession(projectId: string, sessionId: string) {
    return request<{ ok: boolean }>(`/projects/${projectId}/chat-sessions/${sessionId}`, {
      method: "DELETE",
    });
  },
  async uploadChatFile(projectId: string, file: File) {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(uploadUrl(`/projects/${projectId}/chat-uploads`), {
      method: "POST",
      body: formData,
      cache: "no-store",
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `Upload failed: ${response.status}`);
    }
    return response.json() as Promise<ChatUploadResponse>;
  },
  uploadChatFileWithProgress(
    projectId: string,
    file: File,
    onProgress?: (event: UploadProgressEvent) => void,
    signal?: AbortSignal,
  ) {
    return new Promise<ChatUploadResponse>((resolve, reject) => {
      const formData = new FormData();
      formData.append("file", file);
      const xhr = new XMLHttpRequest();
      let settled = false;
      const cleanup = () => {
        signal?.removeEventListener("abort", abortUpload);
      };
      const rejectOnce = (error: Error) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      };
      const abortUpload = () => {
        xhr.abort();
        rejectOnce(new DOMException("Upload aborted.", "AbortError"));
      };
      if (signal?.aborted) {
        rejectOnce(new DOMException("Upload aborted.", "AbortError"));
        return;
      }
      signal?.addEventListener("abort", abortUpload, { once: true });
      xhr.open("POST", uploadUrl(`/projects/${projectId}/chat-uploads`));
      xhr.upload.onprogress = (event) => {
        onProgress?.({
          loaded: event.loaded,
          total: event.lengthComputable ? event.total : undefined,
          lengthComputable: event.lengthComputable,
        });
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            if (settled) return;
            settled = true;
            cleanup();
            resolve(JSON.parse(xhr.responseText) as ChatUploadResponse);
          } catch (error) {
            rejectOnce(error instanceof Error ? error : new Error("Upload response parse failed."));
          }
          return;
        }
        rejectOnce(new Error(xhr.responseText || `Upload failed: ${xhr.status}`));
      };
      xhr.onerror = () => rejectOnce(new Error("Upload failed: network error."));
      xhr.onabort = () => rejectOnce(new DOMException("Upload aborted.", "AbortError"));
      xhr.send(formData);
    });
  },
  getResults(projectId: string) {
    return request<{ accepted: Asset[]; candidate: Asset[]; other: Asset[] }>(`/projects/${projectId}/results`);
  },
  getFiles(projectId: string) {
    return request<ProjectFiles>(`/projects/${projectId}/files`);
  },
  deleteSessionUpload(projectId: string, assetId: string) {
    return request<{ ok: boolean; asset: Asset }>(`/projects/${projectId}/files/session-uploads/${assetId}`, {
      method: "DELETE",
    });
  },
  deleteDataAsset(projectId: string, assetId: string) {
    return request<{ ok: boolean; asset: Asset }>(`/projects/${projectId}/files/assets/${assetId}`, {
      method: "DELETE",
    });
  },
  getResultAsset(projectId: string, assetId: string) {
    return request<AssetDetail>(`/projects/${projectId}/results/${assetId}`);
  },
  compactChatSession(
    projectId: string,
    sessionMessages: ChatSessionMessageRecord[],
    thinkingEffort: "low" | "medium" | "high" = "medium",
    sessionId?: string | null,
  ) {
    return request<{
      compact_id: string;
      summary: string;
      first_kept_message_id: string;
      tokens_before: number;
      tokens_after: number;
      duration_ms: number;
      provider?: string | null;
      model?: string | null;
    }>(`/projects/${projectId}/chat-compact`, {
      method: "POST",
      body: JSON.stringify({
        message: "/compact",
        session_id: sessionId ?? null,
        context: {},
        thinking_effort: thinkingEffort,
        messages: [],
        session_messages: sessionMessages,
      }),
    });
  },
  getReport(projectId: string) {
    return request<{ project: unknown; sections: ReportSection[] }>(`/projects/${projectId}/report`);
  },
  getAdvancedGraph(projectId: string) {
    return request<{ graph: Record<string, unknown>; cards: unknown[] }>(`/projects/${projectId}/advanced/graph`);
  },
  getAdvancedGit(projectId: string) {
    return request<{ items: Array<{ hash: string; date: string; subject: string }> }>(`/projects/${projectId}/advanced/git`);
  },
  getAdvancedProposals(projectId: string) {
    return request<{ items: unknown[] }>(`/projects/${projectId}/advanced/proposals`);
  },
  async streamChat(
    projectId: string,
    message: string,
    thinkingEffort: "low" | "medium" | "high" = "medium",
    messages: ChatHistoryMessage[] = [],
    sessionMessages: ChatSessionMessageRecord[] = [],
    onEvent?: (event: ChatStreamEvent) => void,
    signal?: AbortSignal,
    context: ChatRequestContext = {},
    sessionId?: string | null,
    messageId?: string | null,
  ) {
    const response = await fetch(`${API_BASE}/projects/${projectId}/chat-stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        session_id: sessionId ?? null,
        context,
        thinking_effort: thinkingEffort,
        messages,
        session_messages: sessionMessages,
        message_id: messageId ?? null,
      }),
      cache: "no-store",
      signal,
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `Chat stream failed: ${response.status}`);
    }
    if (!response.body) {
      throw new Error("Chat stream is not available.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const flushBuffer = () => {
      let boundary = buffer.search(/\r?\n\r?\n/);
      while (boundary !== -1) {
        const rawEvent = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + (buffer[boundary] === "\r" ? 4 : 2));
        const payload = rawEvent
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n")
          .trim();
        if (payload) {
          onEvent?.(JSON.parse(payload) as ChatStreamEvent);
        }
        boundary = buffer.search(/\r?\n\r?\n/);
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      flushBuffer();
    }
    buffer += decoder.decode();
    flushBuffer();
  },
  getManagerAuto(projectId: string, sessionId?: string | null) {
    const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
    return request<{ state: ManagerAutoState; is_owner: boolean; btw_mode: boolean }>(`/projects/${projectId}/manager-auto${query}`);
  },
  enableManagerAuto(
    projectId: string,
    sessionId: string,
    mode: "continuous" | "once" = "continuous",
    directiveText?: string | null,
    messageId?: string | null,
  ) {
    return request<{ state: ManagerAutoState; directive?: unknown; wake_event?: Record<string, unknown> | null }>(`/projects/${projectId}/manager-auto`, {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        mode,
        directive_text: directiveText ?? null,
        message_id: messageId ?? null,
        trigger_wake: true,
      }),
    });
  },
  stopManagerAuto(projectId: string, sessionId: string, reason = "user_off", message = "Auto mode 已关闭。") {
    return request<{ state: ManagerAutoState }>(`/projects/${projectId}/manager-auto/stop`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, reason, message }),
    });
  },
  finishAutoEpisode(projectId: string, sessionId: string) {
    return request<{ ok: boolean; state: string; stopped_at?: string; error_code?: string; current_state?: string }>(
      `/projects/${projectId}/manager-auto/finish`,
      { method: "POST", body: JSON.stringify({ session_id: sessionId }) },
    );
  },
  addManagerAutoDirective(projectId: string, sessionId: string, text: string, messageId?: string | null) {
    return request<{ directive: unknown; wake_event: Record<string, unknown> | null; state: ManagerAutoState }>(
      `/projects/${projectId}/manager-auto/directives`,
      {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, text, message_id: messageId ?? null, trigger_wake: true }),
      },
    );
  },
  getRuntimeDependencyJob(projectId: string, jobId: string) {
    return request<{
      job_id: string;
      status: "queued" | "running" | "succeeded" | "failed";
      created_at: string;
      started_at?: string | null;
      finished_at?: string | null;
      payload?: Record<string, unknown> | null;
      result?: Record<string, unknown> | null;
      error?: string | null;
      ok?: boolean | null;
      message?: string | null;
      runtime?: string | null;
      resolved_runtime?: string | null;
      packages?: string[] | null;
      manager?: string | null;
      stdout_tail?: string | null;
      stderr_tail?: string | null;
      status_detail?: string | null;
      changed?: boolean | null;
      phase?: string | null;
    }>(`/projects/${projectId}/runtime-dependency-jobs/${jobId}`);
  },
  resolveRuntimeDependencies(
    projectId: string,
    payload: {
      ecosystem: string;
      runtime: string;
      packages: string[];
      source?: { card_id?: string; run_id?: string } | null;
    },
    sessionId?: string | null,
  ) {
    return request<RuntimeDependencyResolverPlan>(
      `/internal/manager-tools/projects/${projectId}/runtime-dependencies/resolve`,
      {
        method: "POST",
        body: JSON.stringify({ ...payload, source: payload.source ?? {} }),
        headers: sessionId ? { "x-blueprint-session-id": sessionId } : undefined,
      },
    );
  },
  markRuntimeDependencyJobResolved(projectId: string, jobId: string, sessionId: string, resolutionMessage?: string) {
    return request<{
      job_id: string;
      status: string;
      resolution_status: string;
      resolved_at: string;
      resolved_by_session_id: string;
      resolution_message: string;
    }>(`/projects/${projectId}/runtime-dependency-jobs/${jobId}/mark-resolved`, {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        resolution_message: resolutionMessage || "User confirmed the runtime package was installed manually.",
      }),
    });
  },
  acceptProposal(projectId: string, proposalId: string, sessionId?: string | null) {
    return request<{ proposal: Proposal; apply_result: unknown; snapshot: ProjectSnapshot }>(
      `/projects/${projectId}/proposals/${proposalId}/accept`,
      {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId ?? null }),
      },
    );
  },
  modifyProposal(projectId: string, proposalId: string, message: string) {
    return request<{ proposal: Proposal; patch: unknown }>(`/projects/${projectId}/proposals/${proposalId}/modify`, {
      method: "POST",
      body: JSON.stringify({ message, context: {} }),
    });
  },
  rejectProposal(projectId: string, proposalId: string) {
    return request<{ proposal: Proposal }>(`/projects/${projectId}/proposals/${proposalId}/reject`, { method: "POST" });
  },
  startRun(projectId: string, cardId: string, workerType?: string, profileId?: string, pythonRuntime?: string, rRuntime?: string) {
    return request<StartRunResponse>(`/projects/${projectId}/cards/${cardId}/start-run`, {
      method: "POST",
      body: JSON.stringify({ worker_type: workerType ?? null, profile_id: profileId ?? null, python_runtime: pythonRuntime ?? null, r_runtime: rRuntime ?? null }),
    });
  },
  resetCardRunState(projectId: string, cardId: string) {
    return request<{ card_id: string; status: string }>(`/projects/${projectId}/cards/${cardId}/reset-run-state`, {
      method: "POST",
    });
  },
  rerunCard(projectId: string, cardId: string, workerType?: string, profileId?: string, pythonRuntime?: string, rRuntime?: string) {
    return request<StartRunResponse>(`/projects/${projectId}/cards/${cardId}/rerun`, {
      method: "POST",
      body: JSON.stringify({
        worker_type: workerType ?? null,
        profile_id: profileId ?? null,
        python_runtime: pythonRuntime ?? null,
        r_runtime: rRuntime ?? null,
      }),
    });
  },
  getRunEvents(projectId: string, runId: string) {
    return request<{ items: RunEvent[] }>(`/projects/${projectId}/runs/${runId}/events`);
  },
  cancelRun(projectId: string, runId: string, reason?: string) {
    return request<{ run_id: string; status: string; summary: string }>(`/projects/${projectId}/runs/${runId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    });
  },
  cleanupRun(projectId: string, runId: string, reason?: string) {
    return request<{ run_id: string; cleanup_status: string; archived_at?: string | null }>(
      `/projects/${projectId}/runs/${runId}/cleanup`,
      {
        method: "POST",
        body: JSON.stringify({ reason: reason ?? null }),
      },
    );
  },
  getRuntimeApprovals(projectId: string, runId: string) {
    return request<{ items: RuntimeApprovalDecision[] }>(`/projects/${projectId}/runs/${runId}/runtime-approvals`);
  },
  decideRuntimeApproval(projectId: string, runId: string, requestId: string, approve: boolean) {
    return request(`/projects/${projectId}/runs/${runId}/runtime-approvals/${requestId}`, {
      method: "POST",
      body: JSON.stringify({ approve }),
    });
  },
  reviewRun(projectId: string, runId: string, accept = true) {
    return request(`/projects/${projectId}/runs/${runId}/review`, {
      method: "POST",
      body: JSON.stringify({ accept }),
    });
  },
  getManifest(projectId: string, runId: string) {
    return request<{ manifest: unknown; valid: boolean; errors: string[] }>(`/projects/${projectId}/runs/${runId}/manifest`);
  },
  reorderReport(projectId: string, itemIds: string[]) {
    return request<{ project: unknown; sections: ReportSection[] }>(`/projects/${projectId}/report/reorder`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    });
  },
  exportReportHtml(projectId: string) {
    return request<ReportExportResponse>(`/projects/${projectId}/report/export-html`, { method: "POST" });
  },
  exportDiagnostics(projectId: string, maxRuns = 8) {
    return request<DiagnosticExportResponse>(`/projects/${projectId}/diagnostics/export?max_runs=${maxRuns}`, {
      method: "POST",
    });
  },
  getLibrary(kind: "skill" | "mcp") {
    return request<LibraryListResponse>(`/library/${kind === "skill" ? "skills" : "mcp"}`);
  },
  searchLibrary(
    kind: "skill" | "mcp",
    params: { q: string; runtime?: string; tags?: string[]; top_k?: number },
  ) {
    const query = new URLSearchParams();
    query.set("q", params.q);
    if (params.runtime) query.set("runtime", params.runtime);
    for (const tag of params.tags ?? []) {
      query.append("tags", tag);
    }
    if (params.top_k) query.set("top_k", String(params.top_k));
    return request<LibraryListResponse>(`/library/${kind === "skill" ? "skills/search" : "mcp/search"}?${query.toString()}`);
  },
  getLibraryItem(kind: "skill" | "mcp", entryId: string) {
    return request<LibraryDetailResponse>(`/library/${kind === "skill" ? "skills" : "mcp"}/${encodeURIComponent(entryId)}`);
  },
  getSkillLibrary(projectId: string) {
    return request<LibraryListResponse>(`/projects/${projectId}/skill-library`);
  },
  getMcpLibrary(projectId: string) {
    return request<LibraryListResponse>(`/projects/${projectId}/mcp-library`);
  },
  installProjectCapability(
    projectId: string,
    payload: {
      kind: "skill" | "mcp";
      source_type: string;
      source: string;
      overwrite?: boolean;
    },
  ) {
    return request<{
      ok: boolean;
      kind: string;
      installed_id: string;
      installed_name: string;
      summary: string;
      warnings: string[];
    }>(`/projects/${projectId}/capabilities/install`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  async uploadProjectSkill(projectId: string, file: File, overwrite?: boolean) {
    const formData = new FormData();
    formData.append("file", file);
    if (overwrite !== undefined) {
      formData.append("overwrite", String(overwrite));
    }
    const response = await fetch(uploadUrl(`/projects/${projectId}/capabilities/skills/upload`), {
      method: "POST",
      body: formData,
      cache: "no-store",
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `Upload failed: ${response.status}`);
    }
    return response.json() as Promise<{
      ok: boolean;
      kind: string;
      installed_id: string;
      installed_name: string;
      summary: string;
      warnings: string[];
    }>;
  },
  registerProjectMcpServer(
    projectId: string,
    payload: {
      id: string;
      name: string;
      transport: "stdio" | "http" | "sse";
      command?: string;
      args?: string[];
      env?: Record<string, string>;
      url?: string;
      headers?: Record<string, string>;
      overwrite?: boolean;
    },
  ) {
    return request<{
      ok: boolean;
      kind: string;
      installed_id: string;
      installed_name: string;
      summary: string;
      warnings: string[];
    }>(`/projects/${projectId}/capabilities/mcp/register`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  getResultAssetContentUrl(projectId: string, assetId: string) {
    return `${API_BASE}/projects/${projectId}/results/${assetId}/content`;
  },
  getExecutionFileContentUrl(projectId: string, path: string) {
    return `${API_BASE}/projects/${projectId}/files/content?path=${encodeURIComponent(path)}`;
  },
  getRunEventsWsUrl(projectId: string, runId: string) {
    if (typeof window === "undefined") {
      return "";
    }
    if (!API_BASE.startsWith("http")) {
      return "";
    }
    const base = new URL(API_BASE);
    base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    base.pathname = `${base.pathname.replace(/\/$/, "")}/projects/${projectId}/runs/${runId}/ws`;
    base.search = "";
    return base.toString();
  },

  // -----------------------------------------------------------------------
  // Card Library / Blueprint Deck
  // -----------------------------------------------------------------------

  getCardLibrary() {
    return request<CardLibraryListResponse>("/card-library");
  },
  searchCardLibrary(params: { query?: string; tags?: string[]; domain?: string; runtime?: string; top_k?: number }) {
    const sp = new URLSearchParams();
    if (params.query) sp.set("query", params.query);
    (params.tags ?? []).forEach((t) => sp.append("tags", t));
    if (params.domain) sp.set("domain", params.domain);
    if (params.runtime) sp.set("runtime", params.runtime);
    if (params.top_k) sp.set("top_k", String(params.top_k));
    return request<CardLibrarySearchResponse>(`/card-library/search?${sp.toString()}`);
  },
  getCardBlueprint(blueprintId: string) {
    return request<CardBlueprintResponse>(`/card-library/${blueprintId}`);
  },
  saveCardToLibrary(projectId: string, cardId: string) {
    return request<SaveToLibraryResponse>("/card-library", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, card_id: cardId }),
    });
  },
  importCardBlueprint(blueprint: CardBlueprint) {
    return request<SaveToLibraryResponse>("/card-library/import", {
      method: "POST",
      body: JSON.stringify(blueprint),
    });
  },
  updateCardBlueprint(blueprintId: string, updates: { title?: string; summary?: string; tags?: string[]; domain?: string }) {
    return request<CardBlueprintResponse>(`/card-library/${blueprintId}`, {
      method: "PUT",
      body: JSON.stringify(updates),
    });
  },
  deleteCardBlueprint(blueprintId: string) {
    return request<{ ok: boolean; blueprint_id: string }>(`/card-library/${blueprintId}`, {
      method: "DELETE",
    });
  },
  exportCardBlueprint(blueprintId: string) {
    return request<CardBlueprintResponse>(`/card-library/${blueprintId}/export`);
  },
  instantiateCardBlueprint(projectId: string, blueprintId: string, payload: InstantiateBlueprintRequest) {
    return request<InstantiateBlueprintResponse>(
      `/projects/${projectId}/card-library/${blueprintId}/instantiate`,
      { method: "POST", body: JSON.stringify(payload) },
    );
  },

  // -----------------------------------------------------------------------
  // Project Card Library / Blueprint Drafts
  // -----------------------------------------------------------------------

  getProjectCardLibrary(projectId: string) {
    return request<ProjectDraftListResponse>(`/projects/${projectId}/card-library`);
  },
  addCardToProjectLibrary(projectId: string, cardId: string) {
    return request<CreateProjectDraftResponse>(`/projects/${projectId}/card-library`, {
      method: "POST",
      body: JSON.stringify({ card_id: cardId }),
    });
  },
  getProjectCardDraft(projectId: string, draftId: string) {
    return request<ProjectDraftResponse>(`/projects/${projectId}/card-library/${draftId}`);
  },
  reviewProjectCardDraft(projectId: string, draftId: string) {
    return request<{ draft_id: string; status: DraftStatus; review: BlueprintReviewResult }>(
      `/projects/${projectId}/card-library/${draftId}/review`,
      { method: "POST" },
    );
  },
  publishProjectCardDraft(projectId: string, draftId: string) {
    return request<PublishDraftResponse>(`/projects/${projectId}/card-library/${draftId}/publish`, {
      method: "POST",
    });
  },
  updateProjectCardDraft(projectId: string, draftId: string, payload: UpdateProjectDraftRequest) {
    return request<ProjectDraftResponse>(`/projects/${projectId}/card-library/${draftId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  deleteProjectCardDraft(projectId: string, draftId: string) {
    return request<{ ok: boolean; draft_id: string }>(
      `/projects/${projectId}/card-library/${draftId}`,
      { method: "DELETE" },
    );
  },
};
