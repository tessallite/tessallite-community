import { describe, it, expect, beforeEach } from "vitest";
import {
  useModelEditorStore,
  useIsModelDirty,
  useModelNeedsSaveOrDeploy,
  modelNeedsSaveOrDeploy,
} from "./useModelEditorStore";
import { act, renderHook } from "@testing-library/react";

describe("useModelEditorStore", () => {
  beforeEach(() => {
    act(() => {
      useModelEditorStore.getState().clearModel();
    });
  });

  describe("setModel", () => {
    it("initializes model state and clears dirty flag", () => {
      act(() =>
        useModelEditorStore.getState().setModel({
          modelId: "model-1",
          lastSavedVersion: 3,
          deployedVersion: 2,
          lastDeployedAt: "2026-01-01T00:00:00Z",
        }),
      );

      const s = useModelEditorStore.getState();
      expect(s.modelId).toBe("model-1");
      expect(s.lastSavedVersion).toBe(3);
      expect(s.deployedVersion).toBe(2);
      expect(s.lastDeployedAt).toBe("2026-01-01T00:00:00Z");
      expect(s.isDirty).toBe(false);
    });

    it("switching models resets dirty flag", () => {
      act(() => {
        useModelEditorStore.getState().setModel({
          modelId: "model-1",
          lastSavedVersion: 1,
          deployedVersion: null,
          lastDeployedAt: null,
        });
        useModelEditorStore.getState().markDirty();
      });
      expect(useModelEditorStore.getState().isDirty).toBe(true);

      act(() =>
        useModelEditorStore.getState().setModel({
          modelId: "model-2",
          lastSavedVersion: 5,
          deployedVersion: 4,
          lastDeployedAt: "2026-05-01T00:00:00Z",
        }),
      );
      expect(useModelEditorStore.getState().isDirty).toBe(false);
      expect(useModelEditorStore.getState().modelId).toBe("model-2");
    });

    it("re-hydrating the same model preserves the dirty flag and refreshes pointers", () => {
      act(() => {
        useModelEditorStore.getState().setModel({
          modelId: "model-1",
          lastSavedVersion: 1,
          deployedVersion: null,
          lastDeployedAt: null,
        });
        useModelEditorStore.getState().markDirty();
      });
      expect(useModelEditorStore.getState().isDirty).toBe(true);

      // Background refetch of the same model re-runs setModel with new
      // version pointers — isDirty must survive so the user's unsaved
      // table/join edits keep the Save button enabled.
      act(() =>
        useModelEditorStore.getState().setModel({
          modelId: "model-1",
          lastSavedVersion: 2,
          deployedVersion: 1,
          lastDeployedAt: "2026-06-01T00:00:00Z",
        }),
      );

      const s = useModelEditorStore.getState();
      expect(s.isDirty).toBe(true);
      expect(s.lastSavedVersion).toBe(2);
      expect(s.deployedVersion).toBe(1);
      expect(s.lastDeployedAt).toBe("2026-06-01T00:00:00Z");
    });
  });

  describe("clearModel", () => {
    it("resets all state to initial values", () => {
      act(() =>
        useModelEditorStore.getState().setModel({
          modelId: "model-1",
          lastSavedVersion: 3,
          deployedVersion: 2,
          lastDeployedAt: "2026-01-01T00:00:00Z",
        }),
      );
      act(() => useModelEditorStore.getState().clearModel());

      const s = useModelEditorStore.getState();
      expect(s.modelId).toBeNull();
      expect(s.isDirty).toBe(false);
      expect(s.lastSavedVersion).toBeNull();
      expect(s.deployedVersion).toBeNull();
      expect(s.lastDeployedAt).toBeNull();
    });
  });

  describe("markDirty / markClean", () => {
    it("marks state dirty", () => {
      act(() => useModelEditorStore.getState().markDirty());
      expect(useModelEditorStore.getState().isDirty).toBe(true);
    });

    it("marks state clean without changing version pointers", () => {
      act(() => {
        useModelEditorStore.getState().setModel({
          modelId: "m",
          lastSavedVersion: 1,
          deployedVersion: 1,
          lastDeployedAt: "t",
        });
        useModelEditorStore.getState().markDirty();
      });
      act(() => useModelEditorStore.getState().markClean());

      const s = useModelEditorStore.getState();
      expect(s.isDirty).toBe(false);
      expect(s.lastSavedVersion).toBe(1);
      expect(s.deployedVersion).toBe(1);
    });

    it("updates version pointers when cleaning", () => {
      act(() =>
        useModelEditorStore.getState().setModel({
          modelId: "m",
          lastSavedVersion: 1,
          deployedVersion: null,
          lastDeployedAt: null,
        }),
      );
      act(() =>
        useModelEditorStore.getState().markClean({
          lastSavedVersion: 2,
          deployedVersion: 2,
          lastDeployedAt: "2026-05-17T00:00:00Z",
        }),
      );

      const s = useModelEditorStore.getState();
      expect(s.isDirty).toBe(false);
      expect(s.lastSavedVersion).toBe(2);
      expect(s.deployedVersion).toBe(2);
      expect(s.lastDeployedAt).toBe("2026-05-17T00:00:00Z");
    });

    it("partial markClean preserves existing version pointers", () => {
      act(() =>
        useModelEditorStore.getState().setModel({
          modelId: "m",
          lastSavedVersion: 3,
          deployedVersion: 2,
          lastDeployedAt: "old",
        }),
      );
      act(() =>
        useModelEditorStore.getState().markClean({ lastSavedVersion: 4 }),
      );

      const s = useModelEditorStore.getState();
      expect(s.lastSavedVersion).toBe(4);
      expect(s.deployedVersion).toBe(2);
      expect(s.lastDeployedAt).toBe("old");
    });
  });

  describe("useIsModelDirty selector", () => {
    it("returns the isDirty value from the store", () => {
      const { result } = renderHook(() => useIsModelDirty());
      expect(result.current).toBe(false);
      act(() => useModelEditorStore.getState().markDirty());
      expect(result.current).toBe(true);
    });
  });

  describe("modelNeedsSaveOrDeploy (Bug-5515)", () => {
    it("is true when the model has unsaved edits, regardless of versions", () => {
      expect(
        modelNeedsSaveOrDeploy({ isDirty: true, lastSavedVersion: 2, deployedVersion: 2 }),
      ).toBe(true);
    });

    it("is true when the saved version differs from the deployed version", () => {
      expect(
        modelNeedsSaveOrDeploy({ isDirty: false, lastSavedVersion: 3, deployedVersion: 2 }),
      ).toBe(true);
    });

    it("is true when nothing is deployed yet", () => {
      expect(
        modelNeedsSaveOrDeploy({ isDirty: false, lastSavedVersion: 1, deployedVersion: null }),
      ).toBe(true);
    });

    it("is true for a brand-new model with no saved version", () => {
      expect(
        modelNeedsSaveOrDeploy({ isDirty: false, lastSavedVersion: null, deployedVersion: null }),
      ).toBe(true);
    });

    it("is false only when clean AND the saved version equals the deployed version", () => {
      expect(
        modelNeedsSaveOrDeploy({ isDirty: false, lastSavedVersion: 4, deployedVersion: 4 }),
      ).toBe(false);
    });
  });

  describe("useModelNeedsSaveOrDeploy selector", () => {
    it("returns true when dirty and false only when saved AND deployed in sync", () => {
      const { result } = renderHook(() => useModelNeedsSaveOrDeploy());
      // initial: nothing saved/deployed -> needs work
      expect(result.current).toBe(true);

      act(() =>
        useModelEditorStore.getState().setModel({
          modelId: "m",
          lastSavedVersion: 5,
          deployedVersion: 5,
          lastDeployedAt: "t",
        }),
      );
      expect(result.current).toBe(false);

      act(() => useModelEditorStore.getState().markDirty());
      expect(result.current).toBe(true);

      // Save bumps the saved version ahead of the deployed one -> still needs deploy.
      act(() => useModelEditorStore.getState().markClean({ lastSavedVersion: 6 }));
      expect(result.current).toBe(true);

      // Deploy catches up.
      act(() => useModelEditorStore.getState().markClean({ deployedVersion: 6 }));
      expect(result.current).toBe(false);
    });
  });
});
