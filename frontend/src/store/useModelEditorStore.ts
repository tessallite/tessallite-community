import { create } from "zustand";

/**
 * Per-model edit state used by:
 *   - the Model Builder toolbar (Save / Deploy buttons disable when not dirty)
 *   - the unsaved-changes guard (beforeunload + react-router blocker)
 *   - the status badge in the Model Builder header
 *
 * A model-scoped write interceptor (see api/client.ts) calls markDirty()
 * after any successful API write to the open model. Save / Deploy / Revert
 * call markClean() and update the version pointers.
 *
 * The store is keyed by model ID so navigating between models doesn't
 * leak dirty state from one to the other. setModel() only clears isDirty
 * when the model ID changes; calling it again for the same model (on a
 * background refetch) refreshes the version pointers but keeps isDirty.
 */
export type ModelEditorState = {
  modelId: string | null;
  isDirty: boolean;
  // Latest saved version_number for the current model (null = no versions yet)
  lastSavedVersion: number | null;
  // Currently deployed version_number, or null when undeployed
  deployedVersion: number | null;
  lastDeployedAt: string | null;
};

type Actions = {
  setModel(args: {
    modelId: string;
    lastSavedVersion: number | null;
    deployedVersion: number | null;
    lastDeployedAt: string | null;
  }): void;
  clearModel(): void;
  markDirty(): void;
  markClean(args?: {
    lastSavedVersion?: number | null;
    deployedVersion?: number | null;
    lastDeployedAt?: string | null;
  }): void;
};

const initial: ModelEditorState = {
  modelId: null,
  isDirty: false,
  lastSavedVersion: null,
  deployedVersion: null,
  lastDeployedAt: null,
};

export const useModelEditorStore = create<ModelEditorState & Actions>(
  (set) => ({
    ...initial,
    setModel: ({ modelId, lastSavedVersion, deployedVersion, lastDeployedAt }) =>
      set((state) => ({
        modelId,
        // Only clear the dirty flag when the open model actually changes
        // (first load or switching models). The Model Builder re-runs this
        // on every model refetch to refresh the version pointers; a
        // background refetch of the *same* model must not wipe edits the
        // user has made since the last save, so preserve isDirty then.
        isDirty: state.modelId === modelId ? state.isDirty : false,
        lastSavedVersion,
        deployedVersion,
        lastDeployedAt,
      })),
    clearModel: () => set(initial),
    markDirty: () => set({ isDirty: true }),
    markClean: (args) =>
      set((state) => ({
        isDirty: false,
        lastSavedVersion:
          args?.lastSavedVersion !== undefined
            ? args.lastSavedVersion
            : state.lastSavedVersion,
        deployedVersion:
          args?.deployedVersion !== undefined
            ? args.deployedVersion
            : state.deployedVersion,
        lastDeployedAt:
          args?.lastDeployedAt !== undefined
            ? args.lastDeployedAt
            : state.lastDeployedAt,
      })),
  }),
);

/**
 * Tiny convenience selector — components that just want to know whether
 * to show the "edited" pill don't need the whole store state shape.
 */
export const useIsModelDirty = () =>
  useModelEditorStore((s) => s.isDirty);
