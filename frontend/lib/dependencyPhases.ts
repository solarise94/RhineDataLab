export type DependencyJobPhase =
  | "queued"
  | "waiting"
  | "waiting_for_runtime_lock"
  | "building_command"
  | "launching"
  | "launching_subprocess"
  | "running"
  | "running_subprocess"
  | "succeeded"
  | "failed";

export const ACTIVE_DEPENDENCY_PHASES: DependencyJobPhase[] = [
  "queued",
  "waiting",
  "waiting_for_runtime_lock",
  "building_command",
  "launching",
  "launching_subprocess",
  "running",
  "running_subprocess",
];

export interface DependencyPhaseStep {
  readonly step: number;
  readonly label: string;
}

export const DEPENDENCY_PHASE_ALIASES: Record<
  Exclude<DependencyJobPhase, "succeeded" | "failed">,
  DependencyPhaseStep
> = {
  queued: { step: 1, label: "已排队" },
  waiting: { step: 2, label: "等待环境锁" },
  waiting_for_runtime_lock: { step: 2, label: "等待环境锁" },
  building_command: { step: 3, label: "构建命令" },
  launching: { step: 4, label: "启动子进程" },
  launching_subprocess: { step: 4, label: "启动子进程" },
  running: { step: 5, label: "执行中" },
  running_subprocess: { step: 5, label: "执行中" },
};

export function isActiveDependencyPhase(phase?: string | null): boolean {
  if (!phase) return false;
  return (ACTIVE_DEPENDENCY_PHASES as string[]).includes(phase);
}

export function getDependencyPhaseStep(phase?: string | null): DependencyPhaseStep | null {
  if (!phase) return null;
  return DEPENDENCY_PHASE_ALIASES[phase as keyof typeof DEPENDENCY_PHASE_ALIASES] ?? null;
}
