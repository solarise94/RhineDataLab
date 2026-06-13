"use client";

import { useState } from "react";
import { Database, Upload, Trash2, Download, Loader2, CheckCircle2, AlertCircle } from "lucide-react";

import { useReferenceData, useRegisterReferenceData, useDeleteReferenceData } from "@/lib/hooks";
import { api } from "@/lib/api";
import { ReferenceDataKind } from "@/lib/types";

const KINDS: { value: ReferenceDataKind; label: string }[] = [
  { value: "gtf", label: "GTF" },
  { value: "fasta", label: "FASTA" },
  { value: "index", label: "索引文件/压缩包" },
  { value: "annotation", label: "注释" },
  { value: "table", label: "表格" },
  { value: "other", label: "其他" },
];

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ReferenceDataPanel() {
  const { data, isLoading, isError } = useReferenceData();
  const registerMut = useRegisterReferenceData();
  const deleteMut = useDeleteReferenceData();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ReferenceDataKind>("gtf");
  const [description, setDescription] = useState("");
  const [toast, setToast] = useState<{ message: string; kind: "success" | "error" } | null>(null);

  const entries = data?.entries ?? [];

  function showToast(message: string, kind: "success" | "error" = "success") {
    setToast({ message, kind });
    setTimeout(() => setToast(null), 3000);
  }

  function handleUpload() {
    if (!file) {
      showToast("请先选择一个文件", "error");
      return;
    }
    registerMut.mutate(
      { file, name: name.trim() || file.name, kind, description: description.trim() || undefined },
      {
        onSuccess: () => {
          setFile(null);
          setName("");
          setDescription("");
          showToast("已注册参考数据", "success");
        },
        onError: (err) => showToast(err instanceof Error ? err.message : "注册失败", "error"),
      },
    );
  }

  function handleDelete(refId: string, name: string) {
    deleteMut.mutate(refId, {
      onSuccess: () => showToast(`已删除 ${name}`, "success"),
      onError: () => showToast("删除失败", "error"),
    });
  }

  return (
    <div className="card-library-page">
      <div className="card-library-header">
        <div>
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>参考数据</h2>
          <p style={{ margin: 0, color: "var(--muted)", fontSize: 12 }}>
            管理可复用分析卡共用的参考数据（GTF / FASTA / 索引等），声明后随卡复用。
          </p>
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

      <div style={{ padding: "0 16px 12px", display: "grid", gap: 10 }}>
        <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr auto", gap: 8, alignItems: "end" }}>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>文件</span>
            <input
              type="file"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              style={{ fontSize: 12 }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>名称</span>
            <input
              type="text"
              placeholder="（留空用文件名）"
              value={name}
              onChange={(e) => setName(e.target.value)}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <span style={{ fontWeight: 600 }}>类型</span>
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as ReferenceDataKind)}
              style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
            >
              {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
            </select>
          </label>
          <button
            type="button"
            className="btn primary"
            onClick={handleUpload}
            disabled={registerMut.isPending}
            style={{ height: 36 }}
          >
            {registerMut.isPending ? <Loader2 size={14} className="spin" /> : <Upload size={14} />}
            注册
          </button>
        </div>
        <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
          <span style={{ fontWeight: 600 }}>描述（可选）</span>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            style={{ padding: "8px 10px", borderRadius: 6, border: "1px solid var(--line)", background: "var(--bg)", color: "var(--text)" }}
          />
        </label>
      </div>

      <div className="card-library-content">
        {isLoading && <div className="empty-state">加载参考数据…</div>}
        {isError && <div className="empty-state" style={{ color: "var(--red)" }}>参考数据加载失败</div>}
        {!isLoading && !isError && entries.length === 0 && (
          <div className="empty-state">
            <Database size={32} style={{ color: "var(--muted)", marginBottom: 8 }} />
            <p>参考数据注册表为空。上传一个参考文件（如 GTF）开始。</p>
          </div>
        )}
        {!isLoading && !isError && entries.length > 0 && (
          <div className="card-library-content-list" style={{ display: "grid", gap: 8, padding: "0 16px" }}>
            {entries.map((rd) => (
              <div
                key={rd.ref_id}
                style={{
                  display: "grid",
                  gridTemplateColumns: "auto 1fr auto auto",
                  gap: 10,
                  alignItems: "center",
                  padding: "10px 12px",
                  borderRadius: 8,
                  border: "1px solid var(--line)",
                  background: "var(--bg)",
                }}
              >
                <Database size={16} style={{ color: "var(--muted)" }} />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{rd.name} <span className="pill" style={{ marginLeft: 6 }}>{rd.kind}</span></div>
                  <div style={{ color: "var(--muted)", fontSize: 11 }}>
                    {rd.original_filename} · {formatSize(rd.size)} · sha256:{rd.sha256.slice(0, 10)}…
                  </div>
                  {rd.description && <div style={{ color: "var(--muted)", fontSize: 11 }}>{rd.description}</div>}
                </div>
                <a
                  className="btn secondary"
                  href={api.referenceDataDownloadUrl(rd.ref_id)}
                  download
                  style={{ fontSize: 12, padding: "4px 8px", textDecoration: "none" }}
                >
                  <Download size={13} /> 下载
                </a>
                <button
                  type="button"
                  className="btn secondary"
                  style={{ color: "var(--red)", fontSize: 12, padding: "4px 8px" }}
                  onClick={() => handleDelete(rd.ref_id, rd.name)}
                  disabled={deleteMut.isPending}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
