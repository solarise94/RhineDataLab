"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { GitCommit, ShieldAlert } from "lucide-react";
import Editor from "@monaco-editor/react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";

export function AdvancedPanels({
  graph,
  gitItems,
  readOnly = false,
  globalPythonRuntime,
  globalRRuntime,
  projectId,
  sessionId,
}: {
  graph: Record<string, unknown> | null;
  gitItems: Array<{ hash: string; date: string; subject: string }>;
  readOnly?: boolean;
  globalPythonRuntime?: string;
  globalRRuntime?: string;
  projectId: string;
  sessionId?: string | null;
}) {
  const queryClient = useQueryClient();
  const runtimeLabel = globalPythonRuntime && globalPythonRuntime !== "__system__" ? globalPythonRuntime : "系统默认";
  const rRuntimeLabel = globalRRuntime && globalRRuntime !== "__system__" ? globalRRuntime : "系统默认";

  const [ecosystem, setEcosystem] = useState<"python" | "r">("python");
  const [envName, setEnvName] = useState("");
  const [version, setVersion] = useState("");
  const [packages, setPackages] = useState("");
  const [autoSelect, setAutoSelect] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [formSuccess, setFormSuccess] = useState<string | null>(null);

  const createRuntimeMutation = useMutation({
    mutationFn: (payload: {
      ecosystem: "python" | "r";
      env_name: string;
      packages: string[];
      python_version?: string | null;
      r_version?: string | null;
      auto_select: boolean;
      timeout_seconds: number;
      source: { session_id?: string };
    }) => api.createRuntime(projectId, payload, sessionId ?? null),
    onSuccess: async (data) => {
      setFormSuccess(`已提交创建任务：${data.job_id}`);
      setEnvName("");
      setVersion("");
      setPackages("");
      setAutoSelect(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.projectEnvironment(projectId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
    onError: (error: Error) => {
      setFormError(error.message || "创建运行环境失败");
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setFormSuccess(null);
    const trimmedName = envName.trim();
    if (!trimmedName) {
      setFormError("请输入环境名称");
      return;
    }
    if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(trimmedName)) {
      setFormError("环境名称必须以字母开头，只能包含字母、数字、下划线或连字符");
      return;
    }
    const trimmedVersion = version.trim() || undefined;
    if (trimmedVersion && !/^\d+\.\d+(\.\d+)?$/.test(trimmedVersion)) {
      setFormError("版本号格式不正确，例如 3.12 或 4.4");
      return;
    }
    const pkgList = packages
      .split(/[\n,]+/)
      .map((p) => p.trim())
      .filter(Boolean);
    const invalidPkg = pkgList.find((p) => !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(p));
    if (invalidPkg) {
      setFormError(`非法包名：${invalidPkg}`);
      return;
    }
    createRuntimeMutation.mutate({
      ecosystem,
      env_name: trimmedName,
      packages: pkgList,
      ...(ecosystem === "python" ? { python_version: trimmedVersion || null } : { r_version: trimmedVersion || null }),
      auto_select: autoSelect,
      timeout_seconds: 1200,
      source: { session_id: sessionId ?? undefined },
    });
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <ShieldAlert size={16} style={{ color: "var(--purple)" }} />
          技术详情
        </h3>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>诊断</span>
      </div>
      <div className="panel-body stack">
        <div className="meta-block">
          <h4>执行器运行时</h4>
          <div className="kv">
            <div className="meta-text">
              当前项目运行时：Python {runtimeLabel} / R {rRuntimeLabel}
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              {readOnly ? "（只读模式）" : "修改请前往工作台设置。"}单张 card 仍可在执行前覆盖。
            </div>
          </div>
        </div>

        {!readOnly && (
          <div className="meta-block">
            <h4>新建运行环境</h4>
            <form onSubmit={handleSubmit} className="stack" style={{ gap: 12 }}>
              <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
                  <input
                    type="radio"
                    name="ecosystem"
                    value="python"
                    checked={ecosystem === "python"}
                    onChange={() => setEcosystem("python")}
                  />
                  Python
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
                  <input
                    type="radio"
                    name="ecosystem"
                    value="r"
                    checked={ecosystem === "r"}
                    onChange={() => setEcosystem("r")}
                  />
                  R
                </label>
              </div>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                <div style={{ flex: 1, minWidth: 160 }}>
                  <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>环境名称</div>
                  <input
                    type="text"
                    value={envName}
                    onChange={(e) => setEnvName(e.target.value)}
                    placeholder="例如 py311-clean"
                    style={{ width: "100%", padding: "6px 10px", fontSize: 13, borderRadius: 6, border: "1px solid var(--line)" }}
                  />
                </div>
                <div style={{ flex: 1, minWidth: 120 }}>
                  <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{ecosystem === "python" ? "Python 版本" : "R 版本"}</div>
                  <input
                    type="text"
                    value={version}
                    onChange={(e) => setVersion(e.target.value)}
                    placeholder={ecosystem === "python" ? "3.12" : "4.4"}
                    style={{ width: "100%", padding: "6px 10px", fontSize: 13, borderRadius: 6, border: "1px solid var(--line)" }}
                  />
                </div>
              </div>
              <div>
                <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>附加包（可选，用逗号或换行分隔）</div>
                <textarea
                  value={packages}
                  onChange={(e) => setPackages(e.target.value)}
                  placeholder="numpy&#10;pandas"
                  rows={3}
                  style={{ width: "100%", padding: "6px 10px", fontSize: 13, borderRadius: 6, border: "1px solid var(--line)", resize: "vertical" }}
                />
              </div>
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
                <input
                  type="checkbox"
                  checked={autoSelect}
                  onChange={(e) => setAutoSelect(e.target.checked)}
                />
                创建后设为项目默认运行时
              </label>
              {formError && <div style={{ color: "var(--red)", fontSize: 12 }}>{formError}</div>}
              {formSuccess && <div style={{ color: "var(--green)", fontSize: 12 }}>{formSuccess}</div>}
              <button
                type="submit"
                disabled={createRuntimeMutation.isPending}
                style={{
                  alignSelf: "flex-start",
                  padding: "6px 14px",
                  fontSize: 13,
                  borderRadius: 6,
                  border: "none",
                  background: "var(--purple)",
                  color: "#fff",
                  cursor: createRuntimeMutation.isPending ? "not-allowed" : "pointer",
                  opacity: createRuntimeMutation.isPending ? 0.7 : 1,
                }}
              >
                {createRuntimeMutation.isPending ? "创建中..." : "创建运行环境"}
              </button>
            </form>
          </div>
        )}

        <div
          className="meta-block"
          style={{ padding: 0, overflow: "hidden", border: "1px solid var(--line)" }}
        >
          <Editor
            height="360px"
            defaultLanguage="json"
            value={JSON.stringify(graph, null, 2)}
            options={{
              readOnly: true,
              minimap: { enabled: false },
              fontSize: 13,
              wordWrap: "on",
              scrollBeyondLastLine: false,
            }}
            theme="light"
          />
        </div>
        <div className="meta-block">
          <h4 style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <GitCommit size={12} />
            Git 历史
          </h4>
          <div className="stack">
            {gitItems.length ? (
              gitItems.map((item) => (
                <div
                  key={item.hash}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: "8px 10px",
                    borderRadius: 8,
                    background: "var(--panel-2)",
                    fontSize: 12,
                    border: "1px solid var(--line)",
                  }}
                >
                  <code style={{ color: "var(--purple)", fontSize: 11, fontFamily: "monospace" }}>
                    {item.hash.slice(0, 7)}
                  </code>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontWeight: 500, color: "var(--text)" }}>{item.subject}</div>
                    <div className="muted" style={{ fontSize: 11 }}>
                      {item.date}
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <div className="muted" style={{ fontSize: 12 }}>暂无提交记录</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
