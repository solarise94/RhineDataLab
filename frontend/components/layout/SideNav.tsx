"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  FolderGit2,
  Layers,
  Package,
  Beaker,
  MessageSquareText,
  Plus,
  Puzzle,
  Trash2,
  Settings2,
} from "lucide-react";

import { api } from "@/lib/api";
import { useChatSessions, useProjects } from "@/lib/hooks";
import { queryKeys } from "@/lib/query-keys";
import { ScriptPreference, useWorkspaceUiStore } from "@/lib/stores/workspace-ui-store";
import { ChatSessionSummary, ManagerAutoState, PythonRuntime, RRuntime } from "@/lib/types";
import { DependencyJobChip } from "@/components/dependency/DependencyJobChip";

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const media = window.matchMedia(query);
    const handleChange = () => setMatches(media.matches);
    handleChange();
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, [query]);
  return matches;
}

const ARTIFACT_VIEWS = new Set(["results", "files", "report"]);

const primary = [
  { href: "results", label: "文件管理", icon: Package },
  { href: "card-library", label: "牌库", icon: Layers },
  { href: "capabilities", label: "能力中心", icon: Puzzle },
  { href: "settings", label: "工作台设置", icon: Settings2 },
];

function sortSessions(items: ChatSessionSummary[]) {
  return [...items].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

function formatSessionTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function sessionTitle(session: ChatSessionSummary) {
  const summary = session.summary?.trim();
  if (summary && summary !== "新会话") return summary;
  return session.message_count > 0 ? `Session ${session.session_id.slice(-4)}` : "新会话";
}

const EMPTY_SESSIONS: ChatSessionSummary[] = [];

export function SideNav({
  projectId,
  current,
  pythonRuntimes = [],
  rRuntimes = [],
  globalPythonRuntime,
  globalRRuntime,
  scriptPreference = "auto",
  managerAuto,
  currentChatSessionId,
  onSelectGlobalPythonRuntime,
  onSelectGlobalRRuntime,
  onSelectScriptPreference,
}: {
  projectId: string;
  current: string;
  pythonRuntimes?: PythonRuntime[];
  rRuntimes?: RRuntime[];
  globalPythonRuntime?: string;
  globalRRuntime?: string;
  scriptPreference?: ScriptPreference;
  managerAuto?: ManagerAutoState;
  currentChatSessionId?: string | null;
  onSelectGlobalPythonRuntime?: (runtime: string) => void;
  onSelectGlobalRRuntime?: (runtime: string) => void;
  onSelectScriptPreference?: (preference: ScriptPreference) => void;
}) {
  const isMobile = useMediaQuery("(max-width: 1100px)");
  const router = useRouter();
  const queryClient = useQueryClient();
  const sessionsQuery = useChatSessions(projectId, {
    refetchInterval: managerAuto?.enabled ? 6_000 : false,
  });
  const projectsQuery = useProjects();
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const storedCurrentChatSessionId = useWorkspaceUiStore((s) => s.currentChatSessionIdByProject[projectId]);
  const setCurrentChatSessionId = useWorkspaceUiStore((s) => s.setCurrentChatSessionId);
  const clearAttachments = useWorkspaceUiStore((s) => s.clearAttachments);
  const clearDraftMessage = useWorkspaceUiStore((s) => s.clearDraftMessage);
  const sessions = sessionsQuery.data?.items ?? EMPTY_SESSIONS;
  const projects = useMemo(
    () => [...(projectsQuery.data?.items ?? [])].sort((left, right) => left.name.localeCompare(right.name)),
    [projectsQuery.data],
  );
  const currentProject = projects.find((project) => project.project_id === projectId);

  const createSessionMutation = useMutation({
    mutationFn: () => api.createChatSession(projectId),
    onSuccess: ({ session }) => {
      queryClient.setQueryData(queryKeys.chatSession(projectId, session.session_id), { session });
      queryClient.setQueryData(
        queryKeys.chatSessions(projectId),
        (previous: { items: ChatSessionSummary[] } | undefined) => ({
          items: sortSessions([
            {
              session_id: session.session_id,
              summary: session.summary,
              created_at: session.created_at,
              updated_at: session.updated_at,
              revision: session.revision,
              auto_owner: session.auto_owner ?? null,
              auto_mode_state: session.auto_mode_state ?? null,
              btw_mode: session.btw_mode ?? null,
              message_count: session.messages.length,
            },
            ...(previous?.items ?? []).filter((item) => item.session_id !== session.session_id),
          ]),
        }),
      );
      setCurrentChatSessionId(projectId, session.session_id);
      clearAttachments(projectId);
      clearDraftMessage(projectId);
      router.push(`/projects/${projectId}/tasks`);
    },
  });

  const deleteSessionMutation = useMutation({
    mutationFn: (sessionId: string) => api.deleteChatSession(projectId, sessionId),
    onSuccess: (_response, deletedSessionId) => {
      queryClient.setQueryData(queryKeys.chatSessions(projectId), (previous: { items: ChatSessionSummary[] } | undefined) => ({
        items: (previous?.items ?? []).filter((item) => item.session_id !== deletedSessionId),
      }));
      queryClient.removeQueries({ queryKey: queryKeys.chatSession(projectId, deletedSessionId) });
      const remaining = queryClient.getQueryData<{ items: ChatSessionSummary[] }>(queryKeys.chatSessions(projectId))?.items ?? [];
      if (currentChatSessionId === deletedSessionId) {
        if (remaining.length) {
          setCurrentChatSessionId(projectId, remaining[0].session_id);
        } else {
          setCurrentChatSessionId(projectId, null);
          createSessionMutation.mutate();
          return;
        }
      }
      clearAttachments(projectId);
      clearDraftMessage(projectId);
      router.push(`/projects/${projectId}/tasks`);
    },
  });

  useEffect(() => {
    if (sessionsQuery.isLoading || sessionsQuery.isError || createSessionMutation.isPending) {
      return;
    }
    if (!sessions.length) {
      createSessionMutation.mutate();
      return;
    }
    if (!(currentChatSessionId ?? storedCurrentChatSessionId) || !sessions.some((item) => item.session_id === (currentChatSessionId ?? storedCurrentChatSessionId))) {
      setCurrentChatSessionId(projectId, sessions[0].session_id);
    }
  }, [
    createSessionMutation.isPending,
    currentChatSessionId,
    projectId,
    sessions,
    sessionsQuery.isError,
    sessionsQuery.isLoading,
    setCurrentChatSessionId,
  ]);

  function openSession(sessionId: string) {
    setCurrentChatSessionId(projectId, sessionId);
    clearAttachments(projectId);
    clearDraftMessage(projectId);
    router.push(`/projects/${projectId}/tasks`);
  }

  function openProject(nextProjectId: string) {
    setProjectMenuOpen(false);
    if (nextProjectId !== projectId) {
      router.push(`/projects/${nextProjectId}/tasks`);
    }
  }

  return (
    <aside className="side-nav">
      <div className="nav-brand">
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
          <div
            style={{
              width: 34,
              height: 34,
              borderRadius: 10,
              background: "linear-gradient(135deg, #3b82f6, #22c55e)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#fff",
              fontWeight: 700,
              fontSize: 13,
              flexShrink: 0,
              boxShadow: "0 2px 8px rgba(59,130,246,0.3)",
            }}
          >
            <Beaker size={16} />
          </div>
          <div>
            <h1 style={{ fontSize: 15, fontWeight: 700, margin: "0 0 2px", letterSpacing: "-0.2px" }}>
              RhineDataLab
            </h1>
            <p style={{ margin: 0, color: "var(--muted)", fontSize: 11, lineHeight: 1.4 }}>
              生信数据智能平台
            </p>
          </div>
        </div>
      </div>

      <div className="nav-project-switcher">
        <button
          type="button"
          className="nav-project-button"
          onClick={() => setProjectMenuOpen((value) => !value)}
          title="切换项目"
        >
          <Beaker size={16} />
          <span className="nav-project-copy">
            <strong>{currentProject?.name ?? projectId}</strong>
            <span>{projectId}</span>
          </span>
          <ChevronDown size={15} />
        </button>
        {projectMenuOpen ? (
          <div className="nav-project-menu">
            {projects.map((project) => (
              <button
                key={project.project_id}
                type="button"
                className={project.project_id === projectId ? "active" : ""}
                onClick={() => openProject(project.project_id)}
              >
                <Beaker size={14} />
                <span>{project.name}</span>
              </button>
            ))}
            <div className="nav-project-divider" />
            <Link href="/projects" onClick={() => setProjectMenuOpen(false)}>
              管理项目
            </Link>
            <Link href="/projects" onClick={() => setProjectMenuOpen(false)}>
              新建项目
            </Link>
          </div>
        ) : null}
      </div>

      <div className="nav-section-label nav-session-label">
        <span>会话</span>
        <button
          type="button"
          className="nav-session-add"
          onClick={() => createSessionMutation.mutate()}
          disabled={createSessionMutation.isPending}
          title="新建 session"
        >
          <Plus size={14} />
        </button>
      </div>
      <div className="nav-session-list">
        {sessionsQuery.isLoading ? <div className="nav-session-empty">加载会话…</div> : null}
        {sessionsQuery.isError ? <div className="nav-session-empty error">会话加载失败</div> : null}
        {!sessionsQuery.isLoading && !sessionsQuery.isError && !sessions.length ? (
          <div className="nav-session-empty">暂无 session</div>
        ) : null}
        {sessions.map((session) => {
      const effectiveCurrentSessionId = currentChatSessionId ?? storedCurrentChatSessionId;
      const active = effectiveCurrentSessionId === session.session_id;
      const isAutoOwner = Boolean(managerAuto?.enabled && managerAuto.owner_session_id === session.session_id);
      const isBtw = Boolean(managerAuto?.enabled && managerAuto.owner_session_id && managerAuto.owner_session_id !== session.session_id);
          const title = sessionTitle(session);
          return (
            <div key={session.session_id} className={`nav-session-item ${active ? "active" : ""} ${isAutoOwner ? "auto-owner" : ""} ${isBtw ? "btw-session" : ""}`}>
              <button type="button" className="nav-session-main" onClick={() => openSession(session.session_id)} title={session.summary}>
                <MessageSquareText size={14} />
                <span className="nav-session-copy">
                  <strong>
                    {title}
                    {isAutoOwner ? <span className={`nav-session-mode-badge ${managerAuto?.state ?? "idle"}`}>AUTO</span> : null}
                    {!isAutoOwner && isBtw ? <span className="nav-session-mode-badge muted">/btw</span> : null}
                  </strong>
                  <em>{session.message_count} 条消息 · {formatSessionTime(session.updated_at)}</em>
                </span>
              </button>
              <button
                type="button"
                className="nav-session-delete"
                title="删除 session"
                onClick={(event) => {
                  event.stopPropagation();
                  deleteSessionMutation.mutate(session.session_id);
                }}
                disabled={deleteSessionMutation.isPending}
              >
                <Trash2 size={13} />
              </button>
            </div>
          );
        })}
      </div>

      <div className="nav-section-label">工作台</div>
      <div className="nav-links">
        {primary.map((item) => {
          const Icon = item.icon;
          const href = `/projects/${projectId}/${item.href}`;
          const isActive = item.href === "results" ? ARTIFACT_VIEWS.has(current) : current === item.href;
          return (
            <Link key={item.href} href={href} className={`nav-link ${isActive ? "active" : ""}`}>
              <Icon size={16} />
              <span>{item.label}</span>
            </Link>
          );
        })}
      </div>

      <div className="nav-runtime-title">运行时</div>
      <div className="nav-runtime-section">
        <div className="nav-runtime-grid">
          <label className="nav-runtime-field">
            <span>Python</span>
            <select
              value={globalPythonRuntime ?? "__system__"}
              onChange={(event) => onSelectGlobalPythonRuntime?.(event.target.value)}
              disabled={Boolean(managerAuto?.enabled)}
            >
              <option value="__system__">系统默认</option>
              {pythonRuntimes.map((item) => (
                <option key={`${item.manager}:${item.name}`} value={item.name}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="nav-runtime-field">
            <span>R</span>
            <select
              value={globalRRuntime ?? "__system__"}
              onChange={(event) => onSelectGlobalRRuntime?.(event.target.value)}
              disabled={Boolean(managerAuto?.enabled)}
            >
              <option value="__system__">系统默认</option>
              {rRuntimes.map((item) => (
                <option key={`${item.manager}:${item.name}`} value={item.name}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div className="nav-section-label">高级</div>
      <div className="nav-secondary">
        <Link
          href={`/projects/${projectId}/advanced`}
          className={`nav-link ${current === "advanced" ? "active" : ""}`}
        >
          <FolderGit2 size={16} />
          <span>技术详情</span>
        </Link>
        <Link
          href={`/projects/${projectId}/global-card-library`}
          className={`nav-link ${current === "global-card-library" ? "active" : ""}`}
        >
          <Layers size={16} />
          <span>全局牌库管理</span>
        </Link>
      </div>

      <div style={{ marginTop: "auto", paddingTop: 20 }}>
        {!isMobile ? <DependencyJobChip projectId={projectId} className="in-sidenav" /> : null}
        <div
          style={{
            padding: "10px 12px",
            borderRadius: 12,
            background: "var(--green-bg)",
            border: "1px solid var(--green-border)",
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: 12,
            color: "var(--green-dark)",
            fontWeight: 500,
          }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: "var(--green)",
              boxShadow: "0 0 6px rgba(34,197,94,0.4)",
              flexShrink: 0,
            }}
          />
          Manager AI 在线
        </div>
      </div>
    </aside>
  );
}
