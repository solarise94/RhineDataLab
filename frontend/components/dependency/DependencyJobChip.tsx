"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Loader2, CheckCircle2, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import {
  getDependencyPhaseStep,
  isActiveDependencyPhase,
} from "@/lib/dependencyPhases";
import {
  DependencyJobChipState,
  EMPTY_DEPENDENCY_JOBS,
  useWorkspaceUiStore,
} from "@/lib/stores/workspace-ui-store";

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
    () => entries.filter((e) => isActiveDependencyPhase(e.phase || e.status)),
    [entries]
  );
  const activeEntriesRef = useRef(activeEntries);
  activeEntriesRef.current = activeEntries;

  const firstActiveEntry = activeEntries[0];
  const phaseStep = getDependencyPhaseStep(firstActiveEntry?.phase);
  const elapsedSeconds = useElapsed(firstActiveEntry?.startedAt);

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
          const startedAt = job.started_at ? new Date(job.started_at).getTime() : undefined;
          if (startedAt) {
            updateDependencyJob(projectId, entry.jobId, { startedAt });
          }
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
  const chipNode = useMemo<ReactNode>(() => {
    if (activeEntries.length > 0) {
      return (
        <ActiveJobChip
          entry={activeEntries[0]}
          phaseStep={phaseStep}
          elapsedSeconds={elapsedSeconds}
          className={className}
        />
      );
    }
    if (terminalEntries.length > 0) {
      // Show the most recent terminal entry
      const chip = terminalChip(
        terminalEntries.reduce((latest, e) =>
          (e.terminalAt ?? 0) > (latest.terminalAt ?? 0) ? e : latest
        )
      );
      return (
        <div
          className={`dependency-chip ${chip.variant}${
            className ? ` ${className}` : ""
          }`}
        >
          {chip.icon}
          <span>{chip.text}</span>
        </div>
      );
    }
    return null;
  }, [activeEntries, terminalEntries, phaseStep, elapsedSeconds, className]);

  if (!chipNode) return null;

  return chipNode;
}

function ActiveJobChip({
  entry,
  phaseStep,
  elapsedSeconds,
  className,
}: {
  entry: DependencyJobChipState;
  phaseStep: ReturnType<typeof getDependencyPhaseStep>;
  elapsedSeconds: number | null;
  className?: string;
}) {
  const firstPkg = entry.packages?.[0];
  const baseText =
    entry.packages && entry.packages.length === 1 && firstPkg
      ? `正在安装 ${firstPkg}`
      : entry.packages && entry.packages.length > 1
      ? `正在处理 ${entry.packages.length} 个依赖`
      : "依赖处理中";
  const progress = entry.progress;
  const hasProgress = typeof progress === "number" && progress > 0;
  const lastLog = lastLine(entry.stdoutTail || entry.stderrTail);

  if (hasProgress) {
    return (
      <div
        className={`dependency-chip running has-progress${
          className ? ` ${className}` : ""
        }`}
      >
        <div className="dependency-chip-main">
          <Loader2 size={14} className="spinning" />
          <span className="dependency-chip-label">
            {entry.progressLabel || baseText}
          </span>
          <span className="dependency-chip-rate">
            {formatRate(entry.downloadRateBps)}
          </span>
        </div>
        <div className="dependency-chip-progress">
          <div
            style={{
              width: `${Math.min(100, Math.max(0, progress))}%`,
            }}
          />
        </div>
        {lastLog ? <div className="dependency-chip-log">{lastLog}</div> : null}
      </div>
    );
  }

  const parts = [baseText];
  if (phaseStep) {
    parts.push(`步骤 ${phaseStep.step}/5 · ${phaseStep.label}`);
  }
  if (elapsedSeconds !== null) {
    parts.push(`已运行 ${formatElapsed(elapsedSeconds)}`);
  }

  return (
    <div
      className={`dependency-chip running${className ? ` ${className}` : ""}`}
    >
      <Loader2 size={14} className="spinning" />
      <span>{parts.join(" · ")}</span>
    </div>
  );
}

function useElapsed(startedAt?: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!startedAt) return;
    setNow(Date.now());
    const interval = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, [startedAt]);
  return startedAt ? Math.max(0, Math.floor((now - startedAt) / 1_000)) : null;
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return `${mins}m ${secs}s`;
  const hours = Math.floor(mins / 60);
  const remainingMins = mins % 60;
  return `${hours}h ${remainingMins}m`;
}

function formatRate(bps?: number | null): string {
  if (bps == null || bps <= 0) return "";
  if (bps < 1024) return `${bps} B/s`;
  if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} kB/s`;
  if (bps < 1024 * 1024 * 1024) return `${(bps / (1024 * 1024)).toFixed(1)} MB/s`;
  return `${(bps / (1024 * 1024 * 1024)).toFixed(1)} GB/s`;
}

function lastLine(tail?: string | null): string | null {
  if (!tail) return null;
  const lines = tail.split("\n").map((l) => l.trim()).filter(Boolean);
  return lines.length ? lines[lines.length - 1] : null;
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
