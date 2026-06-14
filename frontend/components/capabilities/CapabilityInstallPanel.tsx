"use client";

import { useState } from "react";
import {
  useInstallCapabilityMutation,
  useRegisterMcpServerMutation,
  useUploadSkillMutation,
} from "@/lib/hooks";

type Tab = "skill" | "mcp-local" | "mcp-remote";

interface CapabilityInstallPanelProps {
  projectId: string;
  onInstalled?: (kind: "skill" | "mcp", installedId: string) => void;
}

interface InstallResult {
  ok: boolean;
  installed_id: string;
  installed_name: string;
  summary: string;
  warnings: string[];
}

function kvPairsToRecord(pairs: Array<{ key: string; value: string }>): Record<string, string> {
  const result: Record<string, string> = {};
  for (const { key, value } of pairs) {
    if (key.trim()) {
      result[key.trim()] = value;
    }
  }
  return result;
}

function parseArgsInput(value: string): string[] {
  return value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function CapabilityInstallPanel({ projectId, onInstalled }: CapabilityInstallPanelProps) {
  const installMutation = useInstallCapabilityMutation(projectId);
  const uploadSkillMutation = useUploadSkillMutation(projectId);
  const registerMcpMutation = useRegisterMcpServerMutation(projectId);

  const [tab, setTab] = useState<Tab>("skill");
  const [result, setResult] = useState<InstallResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Skill state
  const [skillMode, setSkillMode] = useState<"upload" | "path">("upload");
  const [skillFile, setSkillFile] = useState<File | null>(null);
  const [skillPath, setSkillPath] = useState("");
  const [overwrite, setOverwrite] = useState(false);

  // MCP local state
  const [mcpId, setMcpId] = useState("");
  const [mcpName, setMcpName] = useState("");
  const [mcpCommand, setMcpCommand] = useState("");
  const [mcpArgs, setMcpArgs] = useState("");
  const [mcpEnv, setMcpEnv] = useState<Array<{ key: string; value: string }>>([{ key: "", value: "" }]);

  // MCP remote state
  const [mcpRemoteId, setMcpRemoteId] = useState("");
  const [mcpRemoteName, setMcpRemoteName] = useState("");
  const [mcpTransport, setMcpTransport] = useState<"http" | "sse">("http");
  const [mcpUrl, setMcpUrl] = useState("");
  const [mcpHeaders, setMcpHeaders] = useState<Array<{ key: string; value: string }>>([{ key: "", value: "" }]);

  function resetFeedback() {
    setError(null);
    setResult(null);
  }

  async function handleInstallSkill(event: React.FormEvent) {
    event.preventDefault();
    resetFeedback();

    if (skillMode === "upload") {
      if (!skillFile) {
        setError("请选择要上传的 .skill 文件。");
        return;
      }
      try {
        const res = await uploadSkillMutation.mutateAsync({ file: skillFile, overwrite });
        setResult(res);
        onInstalled?.("skill", res.installed_id);
      } catch (err) {
        setError(err instanceof Error ? err.message : "上传失败。");
      }
      return;
    }

    if (!skillPath.trim()) {
      setError("请输入 Skill 本地路径。");
      return;
    }
    try {
      const res = await installMutation.mutateAsync({
        kind: "skill",
        source_type: "local_path",
        source: skillPath.trim(),
        overwrite,
      });
      setResult(res);
      onInstalled?.("skill", res.installed_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "安装失败。");
    }
  }

  async function handleRegisterMcpLocal(event: React.FormEvent) {
    event.preventDefault();
    resetFeedback();
    if (!mcpId.trim() || !mcpName.trim() || !mcpCommand.trim()) {
      setError("请填写 MCP Server ID、显示名称和启动命令。");
      return;
    }
    try {
      const res = await registerMcpMutation.mutateAsync({
        id: mcpId.trim(),
        name: mcpName.trim(),
        transport: "stdio",
        command: mcpCommand.trim(),
        args: parseArgsInput(mcpArgs),
        env: kvPairsToRecord(mcpEnv),
        overwrite,
      });
      setResult(res);
      onInstalled?.("mcp", res.installed_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "注册失败。");
    }
  }

  async function handleRegisterMcpRemote(event: React.FormEvent) {
    event.preventDefault();
    resetFeedback();
    if (!mcpRemoteId.trim() || !mcpRemoteName.trim() || !mcpUrl.trim()) {
      setError("请填写 MCP Server ID、显示名称和远程 URL。");
      return;
    }
    try {
      const res = await registerMcpMutation.mutateAsync({
        id: mcpRemoteId.trim(),
        name: mcpRemoteName.trim(),
        transport: mcpTransport,
        url: mcpUrl.trim(),
        headers: kvPairsToRecord(mcpHeaders),
        overwrite,
      });
      setResult(res);
      onInstalled?.("mcp", res.installed_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "注册失败。");
    }
  }

  function addEnvPair() {
    setMcpEnv((items) => [...items, { key: "", value: "" }]);
  }

  function updateEnvPair(index: number, field: "key" | "value", value: string) {
    setMcpEnv((items) => items.map((item, i) => (i === index ? { ...item, [field]: value } : item)));
  }

  function removeEnvPair(index: number) {
    setMcpEnv((items) => items.filter((_, i) => i !== index));
  }

  function addHeaderPair() {
    setMcpHeaders((items) => [...items, { key: "", value: "" }]);
  }

  function updateHeaderPair(index: number, field: "key" | "value", value: string) {
    setMcpHeaders((items) => items.map((item, i) => (i === index ? { ...item, [field]: value } : item)));
  }

  function removeHeaderPair(index: number) {
    setMcpHeaders((items) => items.filter((_, i) => i !== index));
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <h3>安装能力</h3>
        <span>Skill 导入 / MCP Server 注册</span>
      </div>
      <div className="panel-body stack">
        <div className="capability-install-tabs">
          <button
            type="button"
            className={`capability-install-tab ${tab === "skill" ? "active" : ""}`}
            onClick={() => {
              setTab("skill");
              resetFeedback();
            }}
          >
            安装 Skill
          </button>
          <button
            type="button"
            className={`capability-install-tab ${tab === "mcp-local" ? "active" : ""}`}
            onClick={() => {
              setTab("mcp-local");
              resetFeedback();
            }}
          >
            添加本地 MCP Server
          </button>
          <button
            type="button"
            className={`capability-install-tab ${tab === "mcp-remote" ? "active" : ""}`}
            onClick={() => {
              setTab("mcp-remote");
              resetFeedback();
            }}
          >
            添加远程 MCP Server
          </button>
        </div>

        {tab === "skill" ? (
          <form className="stack" onSubmit={handleInstallSkill}>
            <div className="settings-form-grid">
              <label className="settings-field">
                <span>安装方式</span>
                <select value={skillMode} onChange={(e) => setSkillMode(e.target.value as "upload" | "path")}>
                  <option value="upload">上传 .skill 包</option>
                  <option value="path">服务器本地目录</option>
                </select>
              </label>
              {skillMode === "upload" ? (
                <label className="settings-field">
                  <span>Skill 包</span>
                  <input
                    type="file"
                    accept=".skill,.zip"
                    onChange={(e) => setSkillFile(e.target.files?.[0] ?? null)}
                  />
                </label>
              ) : (
                <label className="settings-field">
                  <span>本地路径</span>
                  <input
                    type="text"
                    value={skillPath}
                    onChange={(e) => setSkillPath(e.target.value)}
                    placeholder="例如：workspace/capabilities/my-skill"
                  />
                </label>
              )}
            </div>
            <label className="settings-check">
              <input
                type="checkbox"
                checked={overwrite}
                onChange={(e) => setOverwrite(e.target.checked)}
              />
              <span>已存在时覆盖</span>
            </label>
            <div className="settings-actions">
              <button
                type="submit"
                className="settings-button"
                disabled={
                  uploadSkillMutation.isPending ||
                  installMutation.isPending ||
                  (skillMode === "upload" && !skillFile) ||
                  (skillMode === "path" && !skillPath.trim())
                }
              >
                {uploadSkillMutation.isPending || installMutation.isPending ? "安装中…" : "安装"}
              </button>
            </div>
          </form>
        ) : null}

        {tab === "mcp-local" ? (
          <form className="stack" onSubmit={handleRegisterMcpLocal}>
            <div className="settings-form-grid">
              <label className="settings-field">
                <span>Server ID</span>
                <input
                  type="text"
                  value={mcpId}
                  onChange={(e) => setMcpId(e.target.value)}
                  placeholder="例如：memory"
                />
              </label>
              <label className="settings-field">
                <span>显示名称</span>
                <input
                  type="text"
                  value={mcpName}
                  onChange={(e) => setMcpName(e.target.value)}
                  placeholder="例如：Memory Server"
                />
              </label>
              <label className="settings-field">
                <span>启动命令</span>
                <input
                  type="text"
                  value={mcpCommand}
                  onChange={(e) => setMcpCommand(e.target.value)}
                  placeholder="例如：npx"
                />
              </label>
              <label className="settings-field">
                <span>参数（每行一个，或用逗号分隔）</span>
                <textarea
                  value={mcpArgs}
                  onChange={(e) => setMcpArgs(e.target.value)}
                  placeholder="例如：-y&#10;@modelcontextprotocol/server-memory"
                  rows={3}
                />
              </label>
            </div>
            <div className="settings-field">
              <span>环境变量</span>
              <div className="stack">
                {mcpEnv.map((item, index) => (
                  <div key={index} className="settings-kv-row">
                    <input
                      type="text"
                      placeholder="KEY"
                      value={item.key}
                      onChange={(e) => updateEnvPair(index, "key", e.target.value)}
                    />
                    <input
                      type="text"
                      placeholder="VALUE"
                      value={item.value}
                      onChange={(e) => updateEnvPair(index, "value", e.target.value)}
                    />
                    <button type="button" className="btn secondary" onClick={() => removeEnvPair(index)}>
                      移除
                    </button>
                  </div>
                ))}
                <button type="button" className="btn secondary" onClick={addEnvPair}>
                  添加环境变量
                </button>
              </div>
            </div>
            <label className="settings-check">
              <input
                type="checkbox"
                checked={overwrite}
                onChange={(e) => setOverwrite(e.target.checked)}
              />
              <span>已存在时覆盖</span>
            </label>
            <div className="settings-actions">
              <button
                type="submit"
                className="settings-button"
                disabled={registerMcpMutation.isPending || !mcpId.trim() || !mcpName.trim() || !mcpCommand.trim()}
              >
                {registerMcpMutation.isPending ? "注册中…" : "注册"}
              </button>
            </div>
          </form>
        ) : null}

        {tab === "mcp-remote" ? (
          <form className="stack" onSubmit={handleRegisterMcpRemote}>
            <div className="settings-form-grid">
              <label className="settings-field">
                <span>Server ID</span>
                <input
                  type="text"
                  value={mcpRemoteId}
                  onChange={(e) => setMcpRemoteId(e.target.value)}
                  placeholder="例如：remote-brave"
                />
              </label>
              <label className="settings-field">
                <span>显示名称</span>
                <input
                  type="text"
                  value={mcpRemoteName}
                  onChange={(e) => setMcpRemoteName(e.target.value)}
                  placeholder="例如：Brave Search Remote"
                />
              </label>
              <label className="settings-field">
                <span>传输协议</span>
                <select
                  value={mcpTransport}
                  onChange={(e) => setMcpTransport(e.target.value as "http" | "sse")}
                >
                  <option value="http">HTTP (Streamable)</option>
                  <option value="sse">SSE</option>
                </select>
              </label>
              <label className="settings-field">
                <span>远程 URL</span>
                <input
                  type="text"
                  value={mcpUrl}
                  onChange={(e) => setMcpUrl(e.target.value)}
                  placeholder="例如：https://api.example.com/mcp"
                />
              </label>
            </div>
            <div className="settings-field">
              <span>请求头</span>
              <div className="stack">
                {mcpHeaders.map((item, index) => (
                  <div key={index} className="settings-kv-row">
                    <input
                      type="text"
                      placeholder="Header"
                      value={item.key}
                      onChange={(e) => updateHeaderPair(index, "key", e.target.value)}
                    />
                    <input
                      type="text"
                      placeholder="Value"
                      value={item.value}
                      onChange={(e) => updateHeaderPair(index, "value", e.target.value)}
                    />
                    <button type="button" className="btn secondary" onClick={() => removeHeaderPair(index)}>
                      移除
                    </button>
                  </div>
                ))}
                <button type="button" className="btn secondary" onClick={addHeaderPair}>
                  添加请求头
                </button>
              </div>
            </div>
            <label className="settings-check">
              <input
                type="checkbox"
                checked={overwrite}
                onChange={(e) => setOverwrite(e.target.checked)}
              />
              <span>已存在时覆盖</span>
            </label>
            <div className="settings-actions">
              <button
                type="submit"
                className="settings-button"
                disabled={
                  registerMcpMutation.isPending ||
                  !mcpRemoteId.trim() ||
                  !mcpRemoteName.trim() ||
                  !mcpUrl.trim()
                }
              >
                {registerMcpMutation.isPending ? "注册中…" : "注册"}
              </button>
            </div>
          </form>
        ) : null}

        {error ? <div className="notice-panel error">{error}</div> : null}
        {result ? (
          <div className="notice-panel">
            <div className="settings-kv-list">
              <div>
                <strong>状态</strong>
                <span>{result.ok ? "成功" : "部分成功"}</span>
              </div>
              <div>
                <strong>ID</strong>
                <span>{result.installed_id}</span>
              </div>
              <div>
                <strong>名称</strong>
                <span>{result.installed_name}</span>
              </div>
              <div>
                <strong>说明</strong>
                <span>{result.summary}</span>
              </div>
              {result.warnings.length ? (
                <div>
                  <strong>提示</strong>
                  <span>{result.warnings.join("；")}</span>
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
