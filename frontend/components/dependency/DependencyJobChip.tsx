"use client";

import { useEffect, useMemo, useRef } from "react";
import { Loader2, CheckCircle2, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import { EMPTY_DEPENDENCY_JOBS, useWorkspaceUiStore } from "@/lib/stores/workspace-ui-store";

const DISMISS_AFTER_MS = 2400;

interface DependencyJobChipProps {
  projectId: string;
  className?: string;
}

export function DependencyJobChip({ projectId, className }: DependencyJobChipProps) {
  const jobs = useWorkspaceUiStore(
    (s) => s.dependencyJobsByProject[projectId] ?? EMPTY_DEPENDENCY_JOBS
  );
  const updateDependencyJob = useWorkspaceUiStore((s) => s.updateDependencyJob);
  const removeDependencyJob = useWorkspaceUiStore((s) => s.removeDependencyJob);
  const clearTerminalDependencyJobs = useWorkspaceUiStore(
    (s) => s.clearTerminalDependencyJobs
  );
  const timersRef = useRef<Record<string, number>>({});

  const entries = useMemo(() => Object.values(jobs), [jobs]);

  const activeEntries = useMemo(
    () =>
      entries.filter((e) => {
        const phase = e.phase || e.status;
        return (
          phase === "running" ||
          phase === "queued" ||
          phase === "waiting" ||
          phase === "launching" ||
          phase === "waiting_for_runtime_lock" ||
          phase === "building_command" ||
          phase === "launching_subprocess" ||
          phase === "running_subprocess"
        );
      }),
    [entries]
  );
  const activeEntriesRef = useRef(activeEntries);
  activeEntriesRef.current = activeEntries;

  const terminalEntries = useMemo(
    () =>
      entries.filter(
        (e) => e.status === "succeeded" || e.status === "failed"
      ),
    [entries]
  );

  // Auto-dismiss terminal chips after minimum visible duration
  useEffect(() => {
    if (!terminalEntries.length) return;

    const now = Date.now();
    const activeTimerIds = new Set<string>();

    terminalEntries.forEach((entry) => {
      if (!entry.terminalAt) return;
      activeTimerIds.add(entry.jobId);
      const elapsed = now - entry.terminalAt;
      const delay = Math.max(0, DISMISS_AFTER_MS - elapsed);

      if (delay <= 0) {
        // Already past visible duration; remove immediately if timer not already fired
        if (timersRef.current[entry.jobId]) {
          window.clearTimeout(timersRef.current[entry.jobId]);
          delete timersRef.current[entry.jobId];
        }
        removeDependencyJob(projectId, entry.jobId);
        return;
      }

      if (timersRef.current[entry.jobId]) return; // already scheduled

      timersRef.current[entry.jobId] = window.setTimeout(() => {
        delete timersRef.current[entry.jobId];
        removeDependencyJob(projectId, entry.jobId);
      }, delay);
    });

    // Clean up timers for jobs that are no longer in terminalEntries
    Object.keys(timersRef.current).forEach((jobId) => {
      if (!activeTimerIds.has(jobId)) {
        window.clearTimeout(timersRef.current[jobId]);
        delete timersRef.current[jobId];
      }
    });

    return () => {
      // Unmount cleanup: clear all pending timers to avoid store mutation after unmount
      Object.values(timersRef.current).forEach((id) => window.clearTimeout(id));
      timersRef.current = {};
    };
  }, [terminalEntries, projectId, removeDependencyJob]);

  // Periodic cleanup of orphaned terminal entries
  useEffect(() => {
    const interval = window.setInterval(() => {
      clearTerminalDependencyJobs(projectId);
    }, 30_000);
    return () => window.clearInterval(interval);
  }, [projectId, clearTerminalDependencyJobs]);

  // Compensating sync: poll backend for active jobs in case SSE terminal event was lost.
  // activeEntries is intentionally omitted from the dependency array: reading it via
  // a ref breaks the self-sustaining re-render loop where every store mutation
  // changed activeEntries' identity, tore down the effect, and immediately re-polled.
  useEffect(() => {
    const poll = async () => {
      const entriesToPoll = activeEntriesRef.current;
      if (!entriesToPoll.length) return;
      for (const entry of entriesToPoll) {
        try {
          const job = await api.getRuntimeDependencyJob(projectId, entry.jobId);
          if (job.status === "succeeded" || job.status === "failed") {
            const changed = job.changed ?? null;
            const statusDetail = job.status_detail || undefined;
            const message =
              job.message ||
              (job.status === "succeeded"
                ? changed === false || statusDetail === "already_satisfied"
                  ? "依赖已满足，无需安装"
                  : "依赖安装完成"
                : "依赖安装失败");
            updateDependencyJob(projectId, entry.jobId, {
              status: job.status,
              phase: job.status,
              changed,
              statusDetail,
              message,
              terminalAt: Date.now(),
            });
          }
        } catch {
          // Non-blocking: polling failure should not break the UI.
        }
      }
    };

    poll();
    const interval = window.setInterval(poll, 5_000);
    return () => window.clearInterval(interval);
  }, [projectId, updateDependencyJob]);

  // Pick the single chip to render
  const chip = useMemo(() => {
    if (activeEntries.length > 0) {
      return activeChip(activeEntries);
    }
    if (terminalEntries.length > 0) {
      // Show the most recent terminal entry
      return terminalChip(
        terminalEntries.reduce((latest, e) =>
          (e.terminalAt ?? 0) > (latest.terminalAt ?? 0) ? e : latest
        )
      );
    }
    return null;
  }, [activeEntries, terminalEntries]);

  if (!chip) return null;

  return (
    <div className={`dependency-chip ${chip.variant}${className ? ` ${className}` : ""}`}>
      {chip.icon}
      <span>{chip.text}</span>
    </div>
  );
}

function activeChip(
  entries: Array<{
    packages?: string[];
    runtime?: string;
    jobId: string;
  }>
) {
  const firstPkg = entries[0]?.packages?.[0];
  const text =
    entries.length === 1 && firstPkg
      ? `正在安装 ${firstPkg}...`
      : entries.length === 1
      ? "依赖处理中..."
      : `正在处理 ${entries.length} 个依赖任务`;

  return {
    variant: "running" as const,
    text,
    icon: <Loader2 size={14} className="spinning" />,
  };
}

function terminalChip(
  entry: {
    status: string;
    changed?: boolean | null;
    statusDetail?: string;
    message?: string;
  }
) {
  if (entry.status === "failed") {
    return {
      variant: "error" as const,
      text: entry.message || "依赖安装失败",
      icon: <AlertCircle size={14} />,
    };
  }
  if (entry.changed === false || entry.statusDetail === "already_satisfied") {
    return {
      variant: "info" as const,
      text: entry.message || "依赖已满足，无需安装",
      icon: <CheckCircle2 size={14} />,
    };
  }
  return {
    variant: "success" as const,
    text: entry.message || "依赖安装完成",
    icon: <CheckCircle2 size={14} />,
  };
}
