"use client";

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { ArtifactPreviewRequest } from "@/lib/types";

export type CardPage = "specialist" | "result" | "detail" | "files" | "archive";
export type Attachment = { type: "card" | "asset"; id: string; label: string };
export type ScriptPreference = "auto" | "prefer_python" | "prefer_r" | "prefer_mixed";

export const EMPTY_CARD_PAGE_BY_ID: Record<string, CardPage> = {};
export const EMPTY_ATTACHMENTS: Attachment[] = [];
export const EMPTY_SELECTED_WORKER_BY_CARD: Record<string, string | undefined> = {};
export const EMPTY_SELECTED_PROFILE_BY_CARD: Record<string, string | undefined> = {};
export const EMPTY_PENDING_RUN_RUNTIME_BY_CARD: Record<string, string | undefined> = {};

type ArtifactPreviewStoreState = {
  open: boolean;
  loading: boolean;
  error?: string;
  source?: ArtifactPreviewRequest;
};

export const EMPTY_ARTIFACT_PREVIEW_STATE: ArtifactPreviewStoreState = {
  open: false,
  loading: false,
};

export interface DependencyJobChipState {
  jobId: string;
  status: string;
  phase?: string;
  packages?: string[];
  runtime?: string;
  message?: string;
  statusDetail?: string;
  changed?: boolean | null;
  visible: boolean;
  terminalAt?: number;
}

export const EMPTY_DEPENDENCY_JOBS: Record<string, DependencyJobChipState> = {};

interface WorkspaceUiState {
  currentChatSessionIdByProject: Record<string, string | null | undefined>;
  selectedCardByProject: Record<string, string | null | undefined>;
  cardInteractionOrderByProject: Record<string, string[] | undefined>;
  selectedWorkerByProject: Record<string, Record<string, string | undefined>>;
  selectedProfileByProject: Record<string, Record<string, string | undefined>>;
  globalPythonRuntimeByProject: Record<string, string | undefined>;
  pendingRunPythonRuntimeByProject: Record<string, Record<string, string | undefined>>;
  globalRRuntimeByProject: Record<string, string | undefined>;
  pendingRunRRuntimeByProject: Record<string, Record<string, string | undefined>>;
  scriptPreferenceByProject: Record<string, ScriptPreference | undefined>;
  noticesByProject: Record<string, string | null>;
  expandedCardByProject: Record<string, string | undefined>;
  cardPageByProject: Record<string, Record<string, CardPage>>;
  attachmentsByProject: Record<string, Attachment[]>;
  mobileTabByProject: Record<string, "chat" | "blueprint">;
  draftMessageByProject: Record<string, string>;
  artifactPreviewByProject: Record<string, ArtifactPreviewStoreState | undefined>;
  dependencyJobsByProject: Record<string, Record<string, DependencyJobChipState>>;
  setCurrentChatSessionId: (projectId: string, sessionId?: string | null) => void;
  setSelectedCard: (projectId: string, cardId?: string | null) => void;
  setSelectedWorker: (projectId: string, cardId: string, workerType?: string) => void;
  setSelectedProfile: (projectId: string, cardId: string, profileId?: string) => void;
  setGlobalPythonRuntime: (projectId: string, runtime?: string) => void;
  setPendingRunPythonRuntime: (projectId: string, cardId: string, runtime?: string) => void;
  setGlobalRRuntime: (projectId: string, runtime?: string) => void;
  setPendingRunRRuntime: (projectId: string, cardId: string, runtime?: string) => void;
  setScriptPreference: (projectId: string, preference: ScriptPreference) => void;
  setNotice: (projectId: string, message: string | null) => void;
  setExpandedCard: (projectId: string, cardId?: string) => void;
  setCardPage: (projectId: string, cardId: string, page: CardPage) => void;
  addAttachment: (projectId: string, attachment: Attachment) => void;
  removeAttachment: (projectId: string, id: string) => void;
  clearAttachments: (projectId: string) => void;
  setMobileTab: (projectId: string, tab: "chat" | "blueprint") => void;
  setDraftMessage: (projectId: string, message: string) => void;
  clearDraftMessage: (projectId: string) => void;
  openArtifactPreview: (projectId: string, source: ArtifactPreviewRequest) => void;
  setArtifactPreviewLoading: (projectId: string, loading: boolean) => void;
  setArtifactPreviewError: (projectId: string, error?: string) => void;
  closeArtifactPreview: (projectId: string) => void;
  registerDependencyJob: (projectId: string, job: DependencyJobChipState) => void;
  updateDependencyJob: (projectId: string, jobId: string, updates: Partial<DependencyJobChipState>) => void;
  removeDependencyJob: (projectId: string, jobId: string) => void;
  clearTerminalDependencyJobs: (projectId: string) => void;
}

export const useWorkspaceUiStore = create<WorkspaceUiState>()(
  persist(
    (set) => ({
      currentChatSessionIdByProject: {},
      selectedCardByProject: {},
      cardInteractionOrderByProject: {},
      selectedWorkerByProject: {},
      selectedProfileByProject: {},
      globalPythonRuntimeByProject: {},
      pendingRunPythonRuntimeByProject: {},
      globalRRuntimeByProject: {},
      pendingRunRRuntimeByProject: {},
      scriptPreferenceByProject: {},
      noticesByProject: {},
      expandedCardByProject: {},
      cardPageByProject: {},
      attachmentsByProject: {},
      mobileTabByProject: {},
      draftMessageByProject: {},
      artifactPreviewByProject: {},
      dependencyJobsByProject: {},
      setCurrentChatSessionId: (projectId, sessionId) =>
        set((state) => {
          if (state.currentChatSessionIdByProject[projectId] === sessionId) return state;
          return {
            currentChatSessionIdByProject: {
              ...state.currentChatSessionIdByProject,
              [projectId]: sessionId,
            },
          };
        }),
      setSelectedCard: (projectId, cardId) =>
        set((state) => {
          if (state.selectedCardByProject[projectId] === cardId) return state;
          const priorOrder = state.cardInteractionOrderByProject[projectId] ?? [];
          const nextOrder =
            cardId && cardId.trim()
              ? [...priorOrder.filter((id) => id !== cardId), cardId]
              : priorOrder;
          return {
            selectedCardByProject: {
              ...state.selectedCardByProject,
              [projectId]: cardId,
            },
            cardInteractionOrderByProject: {
              ...state.cardInteractionOrderByProject,
              [projectId]: nextOrder,
            },
          };
        }),
      setSelectedWorker: (projectId, cardId, workerType) =>
        set((state) => {
          if (state.selectedWorkerByProject[projectId]?.[cardId] === workerType) return state;
          return {
            selectedWorkerByProject: {
              ...state.selectedWorkerByProject,
              [projectId]: {
                ...(state.selectedWorkerByProject[projectId] ?? {}),
                [cardId]: workerType,
              },
            },
          };
        }),
      setSelectedProfile: (projectId, cardId, profileId) =>
        set((state) => {
          if (state.selectedProfileByProject[projectId]?.[cardId] === profileId) return state;
          return {
            selectedProfileByProject: {
              ...state.selectedProfileByProject,
              [projectId]: {
                ...(state.selectedProfileByProject[projectId] ?? {}),
                [cardId]: profileId,
              },
            },
          };
        }),
      setGlobalPythonRuntime: (projectId, runtime) =>
        set((state) => {
          if (state.globalPythonRuntimeByProject?.[projectId] === runtime) return state;
          return {
            globalPythonRuntimeByProject: {
              ...(state.globalPythonRuntimeByProject ?? {}),
              [projectId]: runtime,
            },
          };
        }),
      setPendingRunPythonRuntime: (projectId, cardId, runtime) =>
        set((state) => {
          if (state.pendingRunPythonRuntimeByProject?.[projectId]?.[cardId] === runtime) return state;
          return {
            pendingRunPythonRuntimeByProject: {
              ...(state.pendingRunPythonRuntimeByProject ?? {}),
              [projectId]: {
                ...(state.pendingRunPythonRuntimeByProject?.[projectId] ?? {}),
                [cardId]: runtime,
              },
            },
          };
        }),
      setGlobalRRuntime: (projectId, runtime) =>
        set((state) => {
          if (state.globalRRuntimeByProject?.[projectId] === runtime) return state;
          return {
            globalRRuntimeByProject: {
              ...(state.globalRRuntimeByProject ?? {}),
              [projectId]: runtime,
            },
          };
        }),
      setPendingRunRRuntime: (projectId, cardId, runtime) =>
        set((state) => {
          if (state.pendingRunRRuntimeByProject?.[projectId]?.[cardId] === runtime) return state;
          return {
            pendingRunRRuntimeByProject: {
              ...(state.pendingRunRRuntimeByProject ?? {}),
              [projectId]: {
                ...(state.pendingRunRRuntimeByProject?.[projectId] ?? {}),
                [cardId]: runtime,
              },
            },
          };
        }),
      setScriptPreference: (projectId, preference) =>
        set((state) => {
          if (state.scriptPreferenceByProject?.[projectId] === preference) return state;
          return {
            scriptPreferenceByProject: {
              ...(state.scriptPreferenceByProject ?? {}),
              [projectId]: preference,
            },
          };
        }),
      setNotice: (projectId, message) =>
        set((state) => {
          if (state.noticesByProject[projectId] === message) return state;
          return {
            noticesByProject: {
              ...state.noticesByProject,
              [projectId]: message,
            },
          };
        }),
      setExpandedCard: (projectId, cardId) =>
        set((state) => ({
          expandedCardByProject: {
            ...state.expandedCardByProject,
            [projectId]: cardId,
          },
        })),
      setCardPage: (projectId, cardId, page) =>
        set((state) => {
          if (state.cardPageByProject[projectId]?.[cardId] === page) return state;
          return {
            cardPageByProject: {
              ...state.cardPageByProject,
              [projectId]: {
                ...(state.cardPageByProject[projectId] ?? {}),
                [cardId]: page,
              },
            },
          };
        }),
      addAttachment: (projectId, attachment) =>
        set((state) => {
          const list = state.attachmentsByProject[projectId] ?? [];
          if (list.some((a) => a.id === attachment.id)) return state;
          return {
            attachmentsByProject: {
              ...state.attachmentsByProject,
              [projectId]: [...list, attachment],
            },
          };
        }),
      removeAttachment: (projectId, id) =>
        set((state) => ({
          attachmentsByProject: {
            ...state.attachmentsByProject,
            [projectId]: (state.attachmentsByProject[projectId] ?? []).filter((a) => a.id !== id),
          },
        })),
      clearAttachments: (projectId) =>
        set((state) => ({
          attachmentsByProject: {
            ...state.attachmentsByProject,
            [projectId]: [],
          },
        })),
      setMobileTab: (projectId, tab) =>
        set((state) => {
          if (state.mobileTabByProject[projectId] === tab) return state;
          return {
            mobileTabByProject: {
              ...state.mobileTabByProject,
              [projectId]: tab,
            },
          };
        }),
      setDraftMessage: (projectId, message) =>
        set((state) => {
          if (state.draftMessageByProject[projectId] === message) return state;
          return {
            draftMessageByProject: {
              ...state.draftMessageByProject,
              [projectId]: message,
            },
          };
        }),
      clearDraftMessage: (projectId) =>
        set((state) => ({
          draftMessageByProject: {
            ...state.draftMessageByProject,
            [projectId]: "",
          },
        })),
      openArtifactPreview: (projectId, source) =>
        set((state) => ({
          artifactPreviewByProject: {
            ...state.artifactPreviewByProject,
            [projectId]: {
              open: true,
              loading: true,
              error: undefined,
              source,
            },
          },
        })),
      setArtifactPreviewLoading: (projectId, loading) =>
        set((state) => ({
          artifactPreviewByProject: {
            ...state.artifactPreviewByProject,
            [projectId]: {
              ...(state.artifactPreviewByProject[projectId] ?? EMPTY_ARTIFACT_PREVIEW_STATE),
              open: true,
              loading,
            },
          },
        })),
      setArtifactPreviewError: (projectId, error) =>
        set((state) => ({
          artifactPreviewByProject: {
            ...state.artifactPreviewByProject,
            [projectId]: {
              ...(state.artifactPreviewByProject[projectId] ?? EMPTY_ARTIFACT_PREVIEW_STATE),
              open: true,
              loading: false,
              error,
            },
          },
        })),
      closeArtifactPreview: (projectId) =>
        set((state) => ({
          artifactPreviewByProject: {
            ...state.artifactPreviewByProject,
            [projectId]: {
              open: false,
              loading: false,
            },
          },
        })),
      registerDependencyJob: (projectId, job) =>
        set((state) => {
          const projectJobs = state.dependencyJobsByProject[projectId] ?? {};
          if (projectJobs[job.jobId]) return state;
          return {
            dependencyJobsByProject: {
              ...state.dependencyJobsByProject,
              [projectId]: {
                ...projectJobs,
                [job.jobId]: job,
              },
            },
          };
        }),
      updateDependencyJob: (projectId, jobId, updates) =>
        set((state) => {
          const projectJobs = state.dependencyJobsByProject[projectId] ?? {};
          const existing = projectJobs[jobId];
          if (!existing) return state;
          return {
            dependencyJobsByProject: {
              ...state.dependencyJobsByProject,
              [projectId]: {
                ...projectJobs,
                [jobId]: { ...existing, ...updates },
              },
            },
          };
        }),
      removeDependencyJob: (projectId, jobId) =>
        set((state) => {
          const projectJobs = state.dependencyJobsByProject[projectId] ?? {};
          if (!projectJobs[jobId]) return state;
          const next = { ...projectJobs };
          delete next[jobId];
          return {
            dependencyJobsByProject: {
              ...state.dependencyJobsByProject,
              [projectId]: next,
            },
          };
        }),
      clearTerminalDependencyJobs: (projectId) =>
        set((state) => {
          const projectJobs = state.dependencyJobsByProject[projectId] ?? {};
          const active = Object.entries(projectJobs).filter(([, j]) => {
            const phase = j.phase || j.status;
            return (
              phase === "running" ||
              phase === "queued" ||
              phase === "launching" ||
              phase === "waiting" ||
              phase === "waiting_for_runtime_lock" ||
              phase === "building_command" ||
              phase === "launching_subprocess" ||
              phase === "running_subprocess"
            );
          });
          if (active.length === Object.keys(projectJobs).length) return state;
          return {
            dependencyJobsByProject: {
              ...state.dependencyJobsByProject,
              [projectId]: Object.fromEntries(active),
            },
          };
        }),
    }),
    {
      name: "blueprint-workspace-ui-v3",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        currentChatSessionIdByProject: state.currentChatSessionIdByProject,
        selectedCardByProject: state.selectedCardByProject,
        cardInteractionOrderByProject: state.cardInteractionOrderByProject,
        selectedWorkerByProject: state.selectedWorkerByProject,
        selectedProfileByProject: state.selectedProfileByProject,
        globalPythonRuntimeByProject: state.globalPythonRuntimeByProject,
        globalRRuntimeByProject: state.globalRRuntimeByProject,
        scriptPreferenceByProject: state.scriptPreferenceByProject,
        expandedCardByProject: state.expandedCardByProject,
        cardPageByProject: state.cardPageByProject,
        attachmentsByProject: state.attachmentsByProject,
        mobileTabByProject: state.mobileTabByProject,
        draftMessageByProject: state.draftMessageByProject,
        artifactPreviewByProject: state.artifactPreviewByProject,
      }),
    },
  ),
);
