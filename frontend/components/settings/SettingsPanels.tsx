"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronLeft, ChevronRight, Database, Folder, Link2, Loader2, Unlink } from "lucide-react";

import { api } from "@/lib/api";
import {
  useAppSettings,
  useExecutorProfiles,
  useExportDiagnosticsMutation,
  useSaveExecutorProfileMutation,
  useTestApiProviderMutation,
  useUpdateAppSettingsMutation,
  useUpdateProjectRuntimePreferencesMutation,
} from "@/lib/hooks";
import { queryKeys } from "@/lib/query-keys";
import {
  ApiProviderProfile,
  ApiProviderProtocol,
  DataDirectoryMount,
  DiagnosticExportResponse,
  ExecutorProfile,
  ProjectState,
  ProviderBindings,
  ProviderRole,
  PythonRuntime,
  RRuntime,
  TestApiProviderResponse,
  WorkspaceEntry,
  WorkspaceRoot,
} from "@/lib/types";

type ScriptPreference = "auto" | "prefer_python" | "prefer_r" | "prefer_mixed";
type EditableProviderProfile = Omit<ApiProviderProfile, "api_key_configured"> & { api_key_configured?: boolean };

function hasTestResult(
  profile: ApiProviderProfile,
): profile is ApiProviderProfile & { test_result: TestApiProviderResponse } {
  return profile.test_result != null;
}

function testResultsFromProfiles(profiles: ApiProviderProfile[]) {
  return Object.fromEntries(
    profiles
      .filter(hasTestResult)
      .map((profile) => [profile.provider_id, profile.test_result]),
  );
}

function executorAuthModeLabel(authMode: ExecutorProfile["auth_mode"]) {
  return authMode === "project_api" ? "使用应用 API" : "使用执行器原生";
}

const PROVIDER_ROLE_OPTIONS: Array<{
  role: ProviderRole;
  title: string;
  description: string;
  protocols: ApiProviderProtocol[];
}> = [
  {
    role: "manager",
    title: "Manager",
    description: "使用 Anthropic Messages 兼容接口，负责对话、规划和工具调用。",
    protocols: ["anthropic_compatible"],
  },
  {
    role: "reviewer",
    title: "Reviewer",
    description: "使用 Anthropic Messages 兼容接口，对执行结果做只读审计。",
    protocols: ["anthropic_compatible"],
  },
  {
    role: "pi_executor",
    title: "Pi 执行器",
    description: "最佳兼容执行器。当前通过 wrapper 注入 Anthropic-compatible key，并可单独配置 CLI provider base。",
    protocols: ["anthropic_compatible"],
  },
  {
    role: "opencode_executor",
    title: "OpenCode 执行器",
    description: "部分兼容。project_api 注入使用 Anthropic-compatible provider；原生登录模式不会读取这里的 key。",
    protocols: ["anthropic_compatible"],
  },
];

function formatRuntimeLabel(runtime?: string | null) {
  return runtime && runtime !== "__system__" ? runtime : "__system__";
}

function formatProtocolLabel(protocol: ApiProviderProtocol) {
  return protocol === "anthropic_compatible" ? "Anthropic" : "OpenAI";
}

function ExecutorAuthModeSelector({ workerType }: { workerType: string }) {
  const profilesQuery = useExecutorProfiles();
  const saveExecutorProfileMutation = useSaveExecutorProfileMutation();
  const profiles = profilesQuery.data?.profiles ?? [];
  const matrix = profilesQuery.data?.support_matrix;
  const workerProfiles = profiles.filter((profile) => profile.worker_type === workerType);
  const enabledProfiles = workerProfiles.filter((profile) => profile.enabled);
  const preferredAuthMode =
    workerType === "pi" || workerType === "opencode"
      ? "project_api"
      : "cli_native";
  const selectedProfile =
    enabledProfiles.find((profile) => profile.auth_mode === preferredAuthMode)
    ?? enabledProfiles[0]
    ?? workerProfiles.find((profile) => profile.auth_mode === preferredAuthMode)
    ?? workerProfiles[0];
  const selectedAuthMode = selectedProfile?.auth_mode ?? "cli_native";
  const modeProfiles = workerProfiles.filter(
    (profile, index, items) => items.findIndex((item) => item.auth_mode === profile.auth_mode) === index,
  );
  const commandConfigured = matrix?.command_configured?.[workerType] ?? false;

  async function selectAuthMode(authMode: ExecutorProfile["auth_mode"]) {
    const updates = workerProfiles.filter((profile) => profile.enabled !== (profile.auth_mode === authMode));
    await Promise.all(
      updates.map((profile) =>
        saveExecutorProfileMutation.mutateAsync({
          ...profile,
          enabled: profile.auth_mode === authMode,
        }),
      ),
    );
  }

  if (!workerProfiles.length) {
    return <div className="settings-inline-help">当前执行器没有可用 profile，运行会使用后端默认配置。</div>;
  }

  return (
    <>
      <label className="settings-field">
        <span>认证方式</span>
        <select
          value={selectedAuthMode}
          disabled={saveExecutorProfileMutation.isPending || modeProfiles.length <= 1}
          onChange={(event) => void selectAuthMode(event.target.value as ExecutorProfile["auth_mode"])}
        >
          {modeProfiles.map((profile) => (
            <option key={profile.profile_id} value={profile.auth_mode}>
              {executorAuthModeLabel(profile.auth_mode)}
            </option>
          ))}
        </select>
      </label>
      <div className="settings-inline-help">
        {selectedAuthMode === "project_api"
          ? "使用应用 API：从上方 provider 绑定读取 key、模型和 Base URL，由 wrapper 注入执行器。"
          : "使用执行器原生登录方式以兼容 OAuth 登录、cc-switch；wrapper 不注入应用 API。"}
        {" "}
        {commandConfigured ? "CLI 命令已配置。" : "CLI 命令未配置，运行会直接报错。"}
      </div>
    </>
  );
}

function DataDirectorySettingsSection({ projectId, readOnly = false }: { projectId: string; readOnly?: boolean }) {
  const queryClient = useQueryClient();
  const [browserExpanded, setBrowserExpanded] = useState(false);
  const [dataRoots, setDataRoots] = useState<WorkspaceRoot[]>([]);
  const [selectedDataRoot, setSelectedDataRoot] = useState<WorkspaceRoot | null>(null);
  const [dataBrowserPath, setDataBrowserPath] = useState("");
  const [dataBrowserEntries, setDataBrowserEntries] = useState<WorkspaceEntry[]>([]);
  const [dataBrowserLoading, setDataBrowserLoading] = useState(false);
  const [dataBrowserError, setDataBrowserError] = useState<string | null>(null);
  const [mountError, setMountError] = useState<string | null>(null);
  const [mountSuccess, setMountSuccess] = useState<string | null>(null);

  const mountQuery = useQuery({
    queryKey: ["data-directory", projectId],
    queryFn: () => api.getProjectDataDirectory(projectId),
  });

  const mount: DataDirectoryMount | null = mountQuery.data?.data_directory ?? null;
  const isMounted = mount != null;
  const isAvailable = mountQuery.data?.available !== false;

  // Load workspace roots when browser is expanded
  useEffect(() => {
    if (!browserExpanded) return;
    api.listWorkspaceRoots()
      .then((res) => {
        setDataRoots(res.items);
        if (res.items.length > 0 && !selectedDataRoot) {
          setSelectedDataRoot(res.items[0]);
        }
      })
      .catch((err: Error) => {
        setDataBrowserError(err.message);
      });
  }, [browserExpanded]);

  // Load directory entries when root or path changes
  useEffect(() => {
    if (!browserExpanded || !selectedDataRoot) return;
    setDataBrowserLoading(true);
    setDataBrowserError(null);
    let cancelled = false;
    api
      .listWorkspaceEntries(selectedDataRoot.root_id, dataBrowserPath, "directory")
      .then((res) => {
        if (!cancelled) {
          setDataBrowserEntries(res.items);
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setDataBrowserError(err.message);
          setDataBrowserEntries([]);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setDataBrowserLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [browserExpanded, selectedDataRoot, dataBrowserPath]);

  async function handleMount(rootId: string, path: string) {
    setMountError(null);
    setMountSuccess(null);
    if (isMounted) {
      if (!window.confirm("切换数据目录将使原目录下的已注册资产标记为不可用，确认切换？")) {
        return;
      }
    }
    try {
      await api.updateProjectDataDirectory(projectId, { root_id: rootId, path });
      setMountSuccess("数据目录已挂载。");
      setBrowserExpanded(false);
      await queryClient.invalidateQueries({ queryKey: ["data-directory", projectId] });
      await queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.files(projectId) });
      await queryClient.invalidateQueries({ queryKey: [...queryKeys.project(projectId), "export-history"] });
    } catch (err) {
      setMountError(err instanceof Error ? err.message : "挂载失败。");
    }
  }

  async function handleDetach() {
    setMountError(null);
    setMountSuccess(null);
    try {
      await api.deleteProjectDataDirectory(projectId);
      setMountSuccess("数据目录已解除挂载。");
      await queryClient.invalidateQueries({ queryKey: ["data-directory", projectId] });
      await queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.files(projectId) });
      await queryClient.invalidateQueries({ queryKey: [...queryKeys.project(projectId), "export-history"] });
    } catch (err) {
      setMountError(err instanceof Error ? err.message : "解除挂载失败。");
    }
  }

  const dataPathParts = dataBrowserPath ? dataBrowserPath.split("/").filter(Boolean) : [];

  return (
    <section className="settings-section">
      <div className="settings-section-header">
        <div>
          <h3>数据目录</h3>
          <p>挂载一个服务器数据目录到项目，用于输入数据和结果导出。</p>
        </div>
        {isMounted ? (
          <span style={{ display: "flex", alignItems: "center", gap: 8, maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            <Database size={14} />
            {isAvailable ? `已挂载 · ${mount?.path || "."}` : "不可用"}
          </span>
        ) : (
          <span>未挂载</span>
        )}
      </div>

      <div className="panel-body stack">
        {mountError ? <div className="notice-panel error">{mountError}</div> : null}
        {mountSuccess ? <div className="notice-panel success">{mountSuccess}</div> : null}

        {isMounted ? (
          <div className="settings-kv-list">
            <div><strong>根</strong><span>{mount?.root_id}</span></div>
            <div><strong>路径</strong><span>{mount?.path || "."}</span></div>
            <div><strong>解析路径</strong><span>{mount?.resolved_path}</span></div>
            <div><strong>挂载时间</strong><span>{mount?.mounted_at}</span></div>
            <div><strong>状态</strong><span>{isAvailable ? "可用" : "不可用"}</span></div>
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>
            此项目没有挂载数据目录。可以挂载一个已有的服务器数据目录，用于输入数据和结果导出。
          </div>
        )}

        <div className="settings-actions">
          {isMounted ? (
            <>
              <button
                type="button"
                className="settings-button danger"
                onClick={() => {
                  if (window.confirm("解除挂载将移除数据目录关联，但不会删除服务器上的目录。data_mount/ 资产将标记为不可用。确认解除挂载？")) {
                    handleDetach();
                  }
                }}
                disabled={readOnly}
              >
                <Unlink size={14} />
                解除挂载
              </button>
              <button
                type="button"
                className="settings-button secondary"
                onClick={() => setBrowserExpanded((v) => !v)}
                disabled={readOnly}
              >
                <Link2 size={14} />
                {browserExpanded ? "取消" : "切换目录"}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="settings-button"
              onClick={() => setBrowserExpanded((v) => !v)}
              disabled={readOnly}
            >
              <Link2 size={14} />
              {browserExpanded ? "取消" : "挂载数据目录"}
            </button>
          )}
        </div>

        {browserExpanded ? (
          <div className="mount-browser" style={{ marginTop: 12 }}>
            {dataBrowserError ? <div className="notice-panel error">{dataBrowserError}</div> : null}

            <div className="directory-browser-toolbar">
              <select
                value={selectedDataRoot?.root_id ?? ""}
                onChange={(e) => {
                  const root = dataRoots.find((r) => r.root_id === e.target.value);
                  setSelectedDataRoot(root || null);
                  setDataBrowserPath("");
                }}
                disabled={dataBrowserLoading}
              >
                {dataRoots.map((r) => (
                  <option key={r.root_id} value={r.root_id}>
                    {r.label} ({r.path})
                  </option>
                ))}
              </select>
              {selectedDataRoot && (
                <button
                  type="button"
                  className="btn secondary"
                  onClick={() => handleMount(selectedDataRoot.root_id, dataBrowserPath)}
                  disabled={dataBrowserLoading || readOnly}
                >
                  使用当前目录
                </button>
              )}
            </div>

            <div className="directory-browser-breadcrumb">
              <button
                type="button"
                className="breadcrumb-root"
                onClick={() => setDataBrowserPath("")}
                disabled={dataBrowserPath === ""}
              >
                {selectedDataRoot?.label ?? "Root"}
              </button>
              {dataPathParts.map((part, idx) => (
                <span key={idx} className="breadcrumb-part">
                  <span>/</span>
                  <button type="button" onClick={() => setDataBrowserPath(dataPathParts.slice(0, idx + 1).join("/"))}>
                    {part}
                  </button>
                </span>
              ))}
            </div>

            <div className="directory-browser-list" style={{ maxHeight: 240 }}>
              {dataBrowserLoading ? <div className="browser-empty">加载中...</div> : null}
              {!dataBrowserLoading && dataBrowserPath !== "" ? (
                <button
                  type="button"
                  className="browser-entry browser-up"
                  onClick={() => setDataBrowserPath(dataPathParts.slice(0, -1).join("/"))}
                >
                  <ChevronLeft size={16} />
                  ..
                </button>
              ) : null}
              {!dataBrowserLoading && dataBrowserEntries.length === 0 && dataBrowserPath === "" ? (
                <div className="browser-empty">空目录</div>
              ) : null}
              {dataBrowserEntries.map((entry) => (
                <button
                  key={entry.name}
                  type="button"
                  className="browser-entry"
                  onClick={() => setDataBrowserPath(dataBrowserPath ? `${dataBrowserPath}/${entry.name}` : entry.name)}
                >
                  <Folder size={16} />
                  <span className="entry-name">{entry.name}</span>
                  {entry.is_empty ? <span className="entry-badge">空</span> : null}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}

export function SettingsPanels({
  projectId,
  project,
  pythonRuntimes,
  rRuntimes,
  readOnly = false,
}: {
  projectId: string;
  project: ProjectState;
  pythonRuntimes: PythonRuntime[];
  rRuntimes: RRuntime[];
  readOnly?: boolean;
}) {
  const appSettingsQuery = useAppSettings();
  const updateAppSettingsMutation = useUpdateAppSettingsMutation();
  const testApiProviderMutation = useTestApiProviderMutation();
  const updateRuntimeMutation = useUpdateProjectRuntimePreferencesMutation(projectId);
  const exportDiagnosticsMutation = useExportDiagnosticsMutation(projectId);

  const [tavilyKey, setTavilyKey] = useState("");
  const [clearTavilyKey, setClearTavilyKey] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [providerProfiles, setProviderProfiles] = useState<EditableProviderProfile[]>([]);
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({});
  const [clearProviderKeys, setClearProviderKeys] = useState<Record<string, boolean>>({});
  const [editingProviderId, setEditingProviderId] = useState<string | null>(null);
  const [draftProviderIds, setDraftProviderIds] = useState<Record<string, boolean>>({});
  const [testingProviderId, setTestingProviderId] = useState<string | null>(null);
  const [providerTestResults, setProviderTestResults] = useState<Record<string, TestApiProviderResponse>>({});
  const [providerBindings, setProviderBindings] = useState<ProviderBindings>({
    manager: { provider_id: "deepseek" },
    reviewer: { provider_id: "deepseek" },
    pi_executor: { provider_id: "deepseek" },
    opencode_executor: { provider_id: "deepseek" },
    library_summarizer: { provider_id: "deepseek" },
  });
  const [defaultWorkerType, setDefaultWorkerType] = useState("pi");
  const [workerTimeoutSeconds, setWorkerTimeoutSeconds] = useState("1800");
  const [manifestRepairTimeoutSeconds, setManifestRepairTimeoutSeconds] = useState("180");
  const [webSearchEnabled, setWebSearchEnabled] = useState(appSettingsQuery.data?.web_search.enabled ?? false);
  const [tavilyBaseUrl, setTavilyBaseUrl] = useState(appSettingsQuery.data?.web_search.base_url ?? "https://api.tavily.com");
  const [scriptPreference, setScriptPreference] = useState<ScriptPreference>(project.runtime_preferences.script_preference);
  const [pythonRuntime, setPythonRuntime] = useState(formatRuntimeLabel(project.runtime_preferences.python_runtime));
  const [rRuntime, setRRuntime] = useState(formatRuntimeLabel(project.runtime_preferences.r_runtime));
  const [diagnosticInfo, setDiagnosticInfo] = useState<DiagnosticExportResponse | null>(null);

  const runtimeSummary = useMemo(() => {
    const script =
      scriptPreference === "prefer_python"
        ? "偏好 Python"
        : scriptPreference === "prefer_r"
          ? "偏好 R"
          : scriptPreference === "prefer_mixed"
            ? "按任务选择"
            : "让 Manager 询问";
    return `${script} · Python ${pythonRuntime} · R ${rRuntime}`;
  }, [pythonRuntime, rRuntime, scriptPreference]);

  useEffect(() => {
    if (!appSettingsQuery.data) return;
    setProviderProfiles(appSettingsQuery.data.api_provider_profiles);
    setProviderBindings(appSettingsQuery.data.provider_bindings);
    setDefaultWorkerType(appSettingsQuery.data.default_worker_type);
    setWorkerTimeoutSeconds(String(appSettingsQuery.data.worker_timeout_seconds));
    setManifestRepairTimeoutSeconds(String(appSettingsQuery.data.manifest_repair_timeout_seconds));
    setProviderKeys({});
    setClearProviderKeys({});
    setEditingProviderId(null);
    setDraftProviderIds({});
    setTestingProviderId(null);
    setProviderTestResults(testResultsFromProfiles(appSettingsQuery.data.api_provider_profiles));
    setWebSearchEnabled(appSettingsQuery.data.web_search.enabled);
    setTavilyBaseUrl(appSettingsQuery.data.web_search.base_url);
  }, [appSettingsQuery.data]);

  async function saveApiSettings() {
    setStatus(null);
    try {
      const trimmedWorkerTimeout = workerTimeoutSeconds.trim();
      if (!trimmedWorkerTimeout) {
        throw new Error("运行超时时间不能为空。");
      }
      const parsedWorkerTimeout = Number.parseInt(trimmedWorkerTimeout, 10);
      if (!Number.isFinite(parsedWorkerTimeout) || parsedWorkerTimeout < 1) {
        throw new Error("运行超时时间必须是大于等于 1 的整数秒。");
      }

      const trimmedRepairTimeout = manifestRepairTimeoutSeconds.trim();
      if (!trimmedRepairTimeout) {
        throw new Error("修复超时时间不能为空。");
      }
      const parsedRepairTimeout = Number.parseInt(trimmedRepairTimeout, 10);
      if (!Number.isFinite(parsedRepairTimeout) || parsedRepairTimeout < 1) {
        throw new Error("修复超时时间必须是大于等于 1 的整数秒。");
      }

      const apiProviderKeys = Object.fromEntries(
        Object.entries(providerKeys)
          .map(([providerId, value]) => [providerId, value.trim()])
          .filter(([, value]) => value),
      );
      const saved = await updateAppSettingsMutation.mutateAsync({
        api_provider_profiles: providerProfiles.map(({ api_key_configured, test_result, ...profile }) => profile),
        api_provider_keys: apiProviderKeys,
        clear_api_provider_keys: Object.entries(clearProviderKeys)
          .filter(([, checked]) => checked)
          .map(([providerId]) => providerId),
        provider_bindings: providerBindings,
        default_worker_type: defaultWorkerType,
        worker_timeout_seconds: parsedWorkerTimeout,
        manifest_repair_timeout_seconds: parsedRepairTimeout,
        manager_websearch_enabled: webSearchEnabled,
        tavily_api_key: tavilyKey || null,
        clear_tavily_api_key: clearTavilyKey,
        tavily_base_url: tavilyBaseUrl,
      });
      setProviderProfiles(saved.api_provider_profiles);
      setProviderBindings(saved.provider_bindings);
      setDefaultWorkerType(saved.default_worker_type);
      setWorkerTimeoutSeconds(String(saved.worker_timeout_seconds));
      setManifestRepairTimeoutSeconds(String(saved.manifest_repair_timeout_seconds));
      setProviderTestResults(testResultsFromProfiles(saved.api_provider_profiles));
      setEditingProviderId(null);
      setDraftProviderIds({});
      setProviderKeys({});
      setClearProviderKeys({});
      setTavilyKey("");
      setClearTavilyKey(false);
      setStatus("API 设置已保存。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "API 设置保存失败。");
    }
  }

  async function saveRuntimeSettings() {
    setStatus(null);
    try {
      await updateRuntimeMutation.mutateAsync({
        script_preference: scriptPreference,
        python_runtime: pythonRuntime === "__system__" ? null : pythonRuntime,
        r_runtime: rRuntime === "__system__" ? null : rRuntime,
      });
      setStatus("项目运行时偏好已保存。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "运行时偏好保存失败。");
    }
  }

  async function exportDiagnostics() {
    setStatus(null);
    try {
      const response = await exportDiagnosticsMutation.mutateAsync({ maxRuns: 8 });
      setDiagnosticInfo(response);
      setStatus("诊断包已生成。");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "诊断包导出失败。");
    }
  }

  function updateProviderProfile(providerId: string, patch: Partial<EditableProviderProfile>) {
    setProviderProfiles((items) =>
      items.map((item) => (item.provider_id === providerId ? { ...item, ...patch } : item)),
    );
    setProviderTestResults((items) => {
      const next = { ...items };
      delete next[providerId];
      return next;
    });
  }

  function addProviderProfile(protocol: ApiProviderProtocol) {
    const baseId = protocol === "anthropic_compatible" ? "anthropic-provider" : "openai-provider";
    let index = providerProfiles.length + 1;
    let providerId = `${baseId}-${index}`;
    while (providerProfiles.some((item) => item.provider_id === providerId)) {
      index += 1;
      providerId = `${baseId}-${index}`;
    }
    setProviderProfiles((items) => [
      ...items,
      {
        provider_id: providerId,
        display_name: protocol === "anthropic_compatible" ? "Anthropic-compatible Provider" : "OpenAI-compatible Provider",
        protocol,
        model: protocol === "anthropic_compatible" ? "claude-compatible-model" : "gpt-compatible-model",
        base_url: protocol === "anthropic_compatible" ? "https://api.example.com/anthropic" : "https://api.example.com/v1",
        native_base_url: "",
      },
    ]);
    setDraftProviderIds((items) => ({ ...items, [providerId]: true }));
    setEditingProviderId(providerId);
  }

  function removeProviderProfile(providerId: string) {
    const providerToRemove = providerProfiles.find((item) => item.provider_id === providerId);
    if (!providerToRemove) {
      return;
    }
    if (providerProfiles.length <= 1) {
      setStatus("至少保留一个 provider 配置。");
      return;
    }
    const impactedRoles = PROVIDER_ROLE_OPTIONS.filter((option) => nextProviderBindingUsesProvider(providerId, option.role));
    const blockedRoles = impactedRoles.filter(
      (option) => !providerProfiles.some((item) => item.provider_id !== providerId && option.protocols.includes(item.protocol)),
    );
    if (blockedRoles.length) {
      setStatus(`删除 ${providerToRemove.display_name || providerToRemove.provider_id} 前，请先为 ${blockedRoles.map((option) => option.title).join("、")} 选择兼容 provider。`);
      return;
    }
    setProviderProfiles((items) => items.filter((item) => item.provider_id !== providerId));
    setProviderKeys((items) => {
      const next = { ...items };
      delete next[providerId];
      return next;
    });
    setClearProviderKeys((items) => {
      const next = { ...items };
      delete next[providerId];
      return next;
    });
    setEditingProviderId((current) => (current === providerId ? null : current));
    setDraftProviderIds((items) => {
      const next = { ...items };
      delete next[providerId];
      return next;
    });
    setProviderBindings((items) => {
      const next = { ...items };
      for (const option of PROVIDER_ROLE_OPTIONS) {
        if (next[option.role].provider_id !== providerId) continue;
        const fallback = providerProfiles.find((item) => item.provider_id !== providerId && option.protocols.includes(item.protocol));
        next[option.role] = { provider_id: fallback?.provider_id ?? next[option.role].provider_id };
      }
      return next;
    });
  }

  function nextProviderBindingUsesProvider(providerId: string, role: ProviderRole) {
    return providerBindings[role].provider_id === providerId;
  }

  function updateProviderBinding(role: ProviderRole, patch: Partial<ProviderBindings[ProviderRole]>) {
    setProviderBindings((items) => ({
      ...items,
      [role]: {
        ...items[role],
        ...patch,
      },
    }));
  }

  function startEditingProvider(providerId: string) {
    setEditingProviderId(providerId);
  }

  function cancelEditingProvider(providerId: string) {
    if (draftProviderIds[providerId]) {
      setProviderProfiles((items) => items.filter((item) => item.provider_id !== providerId));
      setDraftProviderIds((items) => {
        const next = { ...items };
        delete next[providerId];
        return next;
      });
    } else {
      const savedProfile = appSettingsQuery.data?.api_provider_profiles.find((profile) => profile.provider_id === providerId);
      if (savedProfile) {
        setProviderProfiles((items) => items.map((item) => (item.provider_id === providerId ? savedProfile : item)));
        setProviderTestResults((items) => {
          const next = { ...items };
          if (savedProfile.test_result) {
            next[providerId] = savedProfile.test_result;
          } else {
            delete next[providerId];
          }
          return next;
        });
      }
    }
    setProviderKeys((items) => {
      const next = { ...items };
      delete next[providerId];
      return next;
    });
    setClearProviderKeys((items) => {
      const next = { ...items };
      delete next[providerId];
      return next;
    });
    setEditingProviderId(null);
  }

  function finishEditingProvider(providerId: string) {
    setDraftProviderIds((items) => {
      const next = { ...items };
      delete next[providerId];
      return next;
    });
    setEditingProviderId(null);
    setStatus("Provider 配置已更新，请点击下方「保存 API 设置」生效。");
  }

  async function testProvider(profile: EditableProviderProfile) {
    setTestingProviderId(profile.provider_id);
    setProviderTestResults((items) => {
      const next = { ...items };
      delete next[profile.provider_id];
      return next;
    });
    try {
      const provider = { ...profile };
      delete provider.api_key_configured;
      delete provider.test_result;
      const result = await testApiProviderMutation.mutateAsync({
        provider,
        api_key: providerKeys[profile.provider_id]?.trim() || null,
      });
      setProviderTestResults((items) => ({ ...items, [profile.provider_id]: result }));
    } catch (error) {
      setProviderTestResults((items) => ({
        ...items,
        [profile.provider_id]: {
          ok: false,
          message: error instanceof Error ? error.message : "模型测试失败。",
        },
      }));
    } finally {
      setTestingProviderId((current) => (current === profile.provider_id ? null : current));
    }
  }

  return (
    <div className="stack settings-stack">
      <DataDirectorySettingsSection projectId={projectId} readOnly={readOnly} />
      <section className="settings-section">
        <div className="settings-section-header">
          <div>
            <h3>项目默认运行时</h3>
            <p>新建 card 和未固定 card 将默认使用这里的设置。侧边栏可快速切换；此处可同时修改三项并保存。</p>
          </div>
          <div className="settings-inline-note">{runtimeSummary}</div>
        </div>
        <div className="settings-form-grid">
          <label className="settings-field">
            <span>执行脚本偏好</span>
            <select value={scriptPreference} onChange={(event) => setScriptPreference(event.target.value as ScriptPreference)}>
              <option value="auto">让 Manager 询问</option>
              <option value="prefer_python">偏好 Python</option>
              <option value="prefer_r">偏好 R</option>
              <option value="prefer_mixed">按任务选择</option>
            </select>
          </label>
          <label className="settings-field">
            <span>项目默认 Python</span>
            <select value={pythonRuntime} onChange={(event) => setPythonRuntime(event.target.value)} disabled={readOnly}>
              {pythonRuntimes.map((item) => (
                <option key={`${item.manager}:${item.name}`} value={item.name}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="settings-field">
            <span>项目默认 R</span>
            <select value={rRuntime} onChange={(event) => setRRuntime(event.target.value)} disabled={readOnly}>
              {rRuntimes.map((item) => (
                <option key={`${item.manager}:${item.name}`} value={item.name}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="settings-actions">
          <button type="button" className="settings-button" disabled={readOnly} onClick={saveRuntimeSettings}>
            保存项目默认设置
          </button>
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-section-header">
          <div>
            <h3>API 设置</h3>
            <p>配置 Manager API、执行器项目 API 注入和 Tavily web search。执行器原生登录模式不会读取这里的 key。</p>
          </div>
        </div>
        <div className="settings-provider-grid">
          <div className="settings-provider-card wide">
            <div className="settings-provider-card-header">
              <div>
                <strong>供应商配置</strong>
                <span>先保存 OpenAI-Compatible 或 Anthropic-Compatible provider，再在下方把 provider 分配给 Manager、Reviewer 和执行器。</span>
              </div>
              <em>{providerProfiles.length} providers</em>
            </div>
            <div className="settings-actions">
              <button type="button" className="settings-button secondary" onClick={() => addProviderProfile("anthropic_compatible")}>
                添加 Anthropic-Compatible
              </button>
              <button type="button" className="settings-button secondary" onClick={() => addProviderProfile("openai_compatible")}>
                添加 OpenAI-Compatible
              </button>
            </div>
            <div className="settings-provider-card-grid">
              {providerProfiles.map((provider) => {
                const isEditing = editingProviderId === provider.provider_id;

                if (isEditing) {
                  return (
                    <div key={provider.provider_id} className="settings-provider-edit settings-provider-card wide">
                      <div className="settings-form-grid compact">
                        <label className="settings-field">
                          <span>名称</span>
                          <input
                            value={provider.display_name}
                            onChange={(event) => updateProviderProfile(provider.provider_id, { display_name: event.target.value })}
                          />
                        </label>
                        <label className="settings-field">
                          <span>ID</span>
                          <input
                            value={provider.provider_id}
                            disabled
                            readOnly
                          />
                        </label>
                        <label className="settings-field">
                          <span>协议</span>
                          <select
                            value={provider.protocol}
                            onChange={(event) =>
                              updateProviderProfile(provider.provider_id, { protocol: event.target.value as ApiProviderProtocol })
                            }
                          >
                            <option value="anthropic_compatible">Anthropic-Compatible</option>
                            <option value="openai_compatible">OpenAI-Compatible</option>
                          </select>
                        </label>
                        <label className="settings-field">
                          <span>模型名</span>
                          <input
                            value={provider.model}
                            onChange={(event) => updateProviderProfile(provider.provider_id, { model: event.target.value })}
                            placeholder={provider.protocol === "anthropic_compatible" ? "deepseek-v4-pro / claude-..." : "gpt-4o / qwen-..."}
                          />
                        </label>
                        <label className="settings-field">
                          <span>Base URL</span>
                          <input
                            value={provider.base_url}
                            onChange={(event) => updateProviderProfile(provider.provider_id, { base_url: event.target.value })}
                            placeholder={provider.protocol === "anthropic_compatible" ? "https://api.example.com/anthropic" : "https://api.example.com/v1"}
                          />
                        </label>
                        <label className="settings-field">
                          <span>API key</span>
                          <input
                            type="password"
                            value={providerKeys[provider.provider_id] ?? ""}
                            disabled={clearProviderKeys[provider.provider_id] ?? false}
                            onChange={(event) => {
                              setProviderKeys((items) => ({ ...items, [provider.provider_id]: event.target.value }));
                              setProviderTestResults((items) => {
                                const next = { ...items };
                                delete next[provider.provider_id];
                                return next;
                              });
                            }}
                            placeholder={provider.api_key_configured ? "已配置，留空保持不变" : "输入 API key"}
                          />
                        </label>
                        {provider.protocol === "anthropic_compatible" ? (
                          <label className="settings-field">
                            <span>Native CLI base URL（高级，可选）</span>
                            <input
                              value={provider.native_base_url ?? ""}
                              onChange={(event) => updateProviderProfile(provider.provider_id, { native_base_url: event.target.value })}
                              placeholder="留空自动推导；DeepSeek 通常是 https://api.deepseek.com"
                            />
                          </label>
                        ) : null}
                      </div>
                      {provider.protocol === "anthropic_compatible" ? (
                        <div className="settings-inline-help">
                          Base URL 是 Anthropic-compatible Messages 入口；Native CLI base URL 只给 Pi/sidecar 这类原生 CLI provider 用。留空时会从 Base URL 自动推导。
                        </div>
                      ) : null}
                      <div className="settings-provider-edit-actions">
                        <button
                          type="button"
                          className="settings-button secondary"
                          onClick={() => testProvider(provider)}
                          disabled={testingProviderId === provider.provider_id}
                        >
                          {testingProviderId === provider.provider_id ? "测试中…" : "测试模型"}
                        </button>
                        {provider.api_key_configured ? (
                          <label className="settings-check">
                            <input
                              type="checkbox"
                              checked={clearProviderKeys[provider.provider_id] ?? false}
                              onChange={(event) =>
                                setClearProviderKeys((items) => ({ ...items, [provider.provider_id]: event.target.checked }))
                              }
                            />
                            <span>清除 key</span>
                          </label>
                        ) : null}
                        <div className="settings-provider-edit-buttons">
                          <button type="button" className="settings-button secondary" onClick={() => cancelEditingProvider(provider.provider_id)}>
                            取消
                          </button>
                          <button type="button" className="settings-button" onClick={() => finishEditingProvider(provider.provider_id)}>
                            关闭编辑
                          </button>
                        </div>
                      </div>
                      {providerTestResults[provider.provider_id] ? (
                        <div className={`settings-test-result ${providerTestResults[provider.provider_id].ok ? "ok" : "error"}`}>
                          {providerTestResults[provider.provider_id].message}
                          {providerTestResults[provider.provider_id].latency_ms !== undefined
                            ? ` · ${providerTestResults[provider.provider_id].latency_ms}ms`
                            : ""}
                        </div>
                      ) : null}
                    </div>
                  );
                }

                const testResult = providerTestResults[provider.provider_id];
                const testStatusClass = testResult ? (testResult.ok ? "ok" : "error") : "missing";
                const testStatusTitle = testResult
                  ? testResult.ok
                    ? "模型测试成功"
                    : "模型测试失败"
                  : "尚未测试模型";

                return (
                  <div key={provider.provider_id} className="settings-model-card">
                    <div className="settings-model-card-head">
                      <strong>{provider.display_name || provider.provider_id}</strong>
                      <span
                        className={`settings-model-key-dot ${testStatusClass}`}
                        title={testStatusTitle}
                      />
                    </div>
                    <div className="settings-model-card-model">{provider.model || "—"}</div>
                    <div className="settings-model-card-meta">
                      <span className={`settings-protocol-badge ${provider.protocol}`}>{formatProtocolLabel(provider.protocol)}</span>
                      <span>{provider.api_key_configured ? "key 已配置" : "key 未配置"}</span>
                      <span className="settings-model-card-url">{provider.base_url}</span>
                    </div>
                    <div className="settings-model-card-actions">
                      <button
                        type="button"
                        className="settings-button secondary"
                        onClick={() => testProvider(provider)}
                        disabled={testingProviderId === provider.provider_id}
                      >
                        {testingProviderId === provider.provider_id ? "测试中…" : "测试"}
                      </button>
                      <button type="button" className="settings-button secondary" onClick={() => startEditingProvider(provider.provider_id)}>
                        编辑
                      </button>
                      <button type="button" className="settings-button secondary" onClick={() => removeProviderProfile(provider.provider_id)}>
                        删除
                      </button>
                    </div>
                    {testResult ? (
                      <div className={`settings-test-result ${testResult.ok ? "ok" : "error"}`}>
                        {testResult.ok ? "可用" : "不可用"}
                        {testResult.latency_ms !== undefined
                          ? ` · ${testResult.latency_ms}ms`
                          : ""}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>

          <div className="settings-provider-card wide">
            <div className="settings-provider-card-header">
              <div>
                <strong>角色绑定</strong>
                <span>Manager/Reviewer/Pi/OpenCode project_api 都优先使用 Anthropic-Compatible provider。Pi 兼容性最好，绑定无效时会直接报错。</span>
              </div>
              <em>角色路由</em>
            </div>
            <div className="settings-form-grid compact">
              {PROVIDER_ROLE_OPTIONS.map((option) => {
                const compatibleProviders = providerProfiles.filter((provider) => option.protocols.includes(provider.protocol));
                const binding = providerBindings[option.role];
                return (
                  <div key={option.role} className="settings-role-card">
                    <div>
                      <strong>{option.title}</strong>
                      <span>{option.description}</span>
                    </div>
                    <label className="settings-field">
                      <span>Provider</span>
                      <select
                        value={binding.provider_id}
                        onChange={(event) => updateProviderBinding(option.role, { provider_id: event.target.value })}
                      >
                        {compatibleProviders.map((provider) => (
                          <option key={provider.provider_id} value={provider.provider_id}>
                            {provider.display_name} · {provider.protocol}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="settings-inline-help">
                      模型名来自所选供应商：{" "}
                      {providerProfiles.find((provider) => provider.provider_id === binding.provider_id)?.model || "未配置"}
                    </div>
                    {!compatibleProviders.length ? (
                      <div className="settings-inline-help">暂无兼容 provider，请先添加 {option.protocols.join(" / ")}。</div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>

          <div className="settings-provider-card">
            <div className="settings-provider-card-header">
              <div>
                <strong>默认执行器</strong>
                <span>运行任务时优先使用的执行器。如果所选执行器未配置，运行会直接报错。</span>
              </div>
              <em>默认执行器</em>
            </div>
            <div className="settings-form-grid compact">
              <label className="settings-field">
                <span>执行器</span>
                <select value={defaultWorkerType} onChange={(event) => setDefaultWorkerType(event.target.value)}>
                  {(() => {
                    const available = new Set(appSettingsQuery.data?.available_executors ?? []);
                    return [
                      { value: "pi", label: "Pi" },
                      { value: "opencode", label: "OpenCode" },
                      { value: "codex", label: "Codex" },
                      { value: "claude_code", label: "Claude Code" },
                    ].map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}{available.has(option.value) ? "" : "（未配置）"}
                      </option>
                    ));
                  })()}
                </select>
              </label>
              <ExecutorAuthModeSelector workerType={defaultWorkerType} />
              {appSettingsQuery.data && !appSettingsQuery.data.available_executors.includes(defaultWorkerType) ? (
                <div className="settings-inline-help" style={{ color: "#e07020" }}>
                  当前选择的 {defaultWorkerType} 未配置，运行时会直接报错。请在部署脚本的 backend.env 中设置 BLUEPRINT_
                  {defaultWorkerType.toUpperCase()}_COMMAND_JSON。
                </div>
              ) : (
                <div className="settings-inline-help">
                  当前可用：{appSettingsQuery.data?.available_executors.length
                    ? appSettingsQuery.data.available_executors.join(", ")
                    : "无（请在环境变量中配置 *_COMMAND 或 *_COMMAND_JSON）"}
                </div>
              )}
            </div>
          </div>

          <div className="settings-provider-card">
            <div className="settings-provider-card-header">
              <div>
                <strong>执行超时</strong>
                <span>后台 card run 的系统级执行上限，单位秒。超时后 backend 会终止 executor 进程。</span>
              </div>
              <em>{workerTimeoutSeconds || "—"}s</em>
            </div>
            <div className="settings-form-grid compact">
              <label className="settings-field">
                <span>超时时间（秒）</span>
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={workerTimeoutSeconds}
                  onChange={(event) => setWorkerTimeoutSeconds(event.target.value)}
                />
              </label>
              <div className="settings-inline-help">
                当前设置会写入应用级配置，并覆盖默认的 `BLUEPRINT_WORKER_TIMEOUT_SECONDS` 运行时值。
              </div>
            </div>
          </div>

          <div className="settings-provider-card">
            <div className="settings-provider-card-header">
              <div>
                <strong>修复超时</strong>
                <span>Manifest 校验失败后，重启 executor 进行修复的单次超时上限，单位秒。</span>
              </div>
              <em>{manifestRepairTimeoutSeconds || "—"}s</em>
            </div>
            <div className="settings-form-grid compact">
              <label className="settings-field">
                <span>超时时间（秒）</span>
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={manifestRepairTimeoutSeconds}
                  onChange={(event) => setManifestRepairTimeoutSeconds(event.target.value)}
                />
              </label>
              <div className="settings-inline-help">
                当前设置会写入应用级配置，并覆盖默认的 `BLUEPRINT_MANIFEST_REPAIR_TIMEOUT_SECONDS` 运行时值。
              </div>
            </div>
          </div>

          <div className="settings-provider-card">
            <div className="settings-provider-card-header">
              <div>
                <strong>网络搜索</strong>
                <span>Tavily 仅供 Manager 搜索使用，不参与执行器 API 注入。</span>
              </div>
              <em>{webSearchEnabled ? "已启用" : "已禁用"}</em>
            </div>
            <div className="settings-form-grid compact">
              <label className="settings-field">
                <span>Tavily key</span>
                <input
                  type="password"
                  value={tavilyKey}
                  onChange={(event) => setTavilyKey(event.target.value)}
                  disabled={clearTavilyKey}
                  placeholder={appSettingsQuery.data?.web_search.api_key_configured ? "已配置，留空保持不变" : "输入 Tavily API key"}
                />
              </label>
              {appSettingsQuery.data?.web_search.api_key_configured ? (
                <label className="settings-check">
                  <input
                    type="checkbox"
                    checked={clearTavilyKey}
                    onChange={(event) => setClearTavilyKey(event.target.checked)}
                  />
                  <span>清除已保存的 Tavily key</span>
                </label>
              ) : null}
              <label className="settings-field">
                <span>Tavily base</span>
                <input value={tavilyBaseUrl} onChange={(event) => setTavilyBaseUrl(event.target.value)} />
              </label>
              <label className="settings-check">
                <input type="checkbox" checked={webSearchEnabled} onChange={(event) => setWebSearchEnabled(event.target.checked)} />
                <span>启用 Tavily web search</span>
              </label>
            </div>
          </div>
        </div>
        <div className="settings-actions">
          <button type="button" className="settings-button" onClick={saveApiSettings}>
            保存 API 设置
          </button>
        </div>
        {status ? <div className="settings-status">{status}</div> : null}
      </section>

      <section className="settings-section">
        <div className="settings-section-header">
          <div>
            <h3>诊断</h3>
            <p>导出脱敏诊断包，包含会话、最近运行日志、错误摘要和配置概览，方便协作者回传排查。</p>
          </div>
        </div>
        <div className="settings-actions">
          <button type="button" className="settings-button" onClick={exportDiagnostics} disabled={exportDiagnosticsMutation.isPending}>
            {exportDiagnosticsMutation.isPending ? "正在导出…" : "导出诊断包"}
          </button>
          {diagnosticInfo ? (
            <a className="settings-button secondary" href={diagnosticInfo.download_url}>
              下载诊断包
            </a>
          ) : null}
        </div>
        {diagnosticInfo ? (
          <div className="settings-kv-list">
            <div><strong>导出时间</strong><span>{diagnosticInfo.created_at}</span></div>
            <div><strong>包含 runs</strong><span>{diagnosticInfo.run_count}</span></div>
            <div><strong>包含 sessions</strong><span>{diagnosticInfo.session_count}</span></div>
            <div><strong>保存路径</strong><span>{diagnosticInfo.path}</span></div>
          </div>
        ) : null}
      </section>

    </div>
  );
}
