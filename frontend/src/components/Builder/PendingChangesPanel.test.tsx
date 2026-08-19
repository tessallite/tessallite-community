import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import PendingChangesPanel from "./PendingChangesPanel";
import { useModelEditorStore } from "../../store/useModelEditorStore";

// G-013-01 (Bug-9171): the pending-change surface must (1) correctly PARTITION
// unsaved edits from saved-but-undeployed changes, (2) DISCARD-to-saved and
// DISCARD-to-deployed must actually REVERT the editor store state, and (3)
// discards are gated behind confirmation.

const pendingMock = vi.fn();
const listMock = vi.fn();
const discardMock = vi.fn();
const revertMock = vi.fn();

vi.mock("../../api/versionsApi", async () => {
  const actual = await vi.importActual<typeof import("../../api/versionsApi")>(
    "../../api/versionsApi",
  );
  return {
    ...actual,
    usePendingChanges: (...args: unknown[]) => pendingMock(...args),
    versionsApi: {
      list: (...args: unknown[]) => listMock(...args),
      discardDraft: (...args: unknown[]) => discardMock(...args),
      revert: (...args: unknown[]) => revertMock(...args),
    },
  };
});

const confirmMock = vi.fn().mockResolvedValue(true);
vi.mock("../Confirm", () => ({ useConfirm: () => confirmMock }));

const PROJECT_ID = "proj-1";
const MODEL_ID = "model-1";
const DEPLOYED_VERSION_ID = "dep-1111";

/** A measure "added" diff row for one named measure. */
function measureAdded(slug: string) {
  return {
    measures: { added: [{ id: slug, slug }], removed: [], changed: [] },
  };
}

function renderPanel(canRevert = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PendingChangesPanel
        projectId={PROJECT_ID}
        modelId={MODEL_ID}
        canRevert={canRevert}
      />
    </QueryClientProvider>,
  );
}

/** Seed the real store: saved v2, deployed v1, with unsaved edits present. */
function seedDirtyStore() {
  act(() => {
    useModelEditorStore.getState().clearModel();
    useModelEditorStore.getState().setModel({
      modelId: MODEL_ID,
      lastSavedVersion: 2,
      deployedVersion: 1,
      lastDeployedAt: "2026-01-01T00:00:00Z",
    });
    useModelEditorStore.getState().markDirty();
  });
}

describe("PendingChangesPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    confirmMock.mockResolvedValue(true);
    discardMock.mockResolvedValue({ status: "ok", restored_to_version: 2 });
    revertMock.mockResolvedValue({
      status: "ok",
      reverted_to: DEPLOYED_VERSION_ID,
      governance_preserved: true,
      governance_preserved_note: "Governance preserved.",
    });
    listMock.mockResolvedValue([
      { id: DEPLOYED_VERSION_ID, version_number: 1, summary: null, created_at: "", created_by: "", is_deployed: true },
      { id: "v2", version_number: 2, summary: null, created_at: "", created_by: "", is_deployed: false },
    ]);
    // Default: an unsaved draft measure AND a distinct saved-but-undeployed one.
    pendingMock.mockReturnValue({
      isLoading: false,
      isError: false,
      data: {
        unsaved: {
          base_version: 2,
          base_version_unavailable: false,
          diff: measureAdded("draft_measure"),
        },
        saved_undeployed: {
          deployed_version: 1,
          saved_version: 2,
          diff: measureAdded("saved_measure"),
        },
      },
    });
  });

  it("partitions unsaved edits from saved-but-undeployed changes", async () => {
    seedDirtyStore();
    renderPanel();

    // Both section headers render.
    expect(screen.getByText("Unsaved edits")).toBeInTheDocument();
    expect(screen.getByText("Saved but not deployed")).toBeInTheDocument();
    // Base-version labels are partitioned correctly.
    expect(screen.getByText(/compared to saved v2/i)).toBeInTheDocument();
    expect(screen.getByText(/compared to deployed v1/i)).toBeInTheDocument();

    // Review the unsaved set: it shows the DRAFT measure, not the saved one.
    const reviewButtons = screen.getAllByRole("button", { name: /review changes/i });
    await userEvent.click(reviewButtons[0]);
    expect(await screen.findByText("draft_measure")).toBeInTheDocument();
  });

  it("discard-to-saved reverts the store to the saved baseline (isDirty=false, pointers unchanged)", async () => {
    seedDirtyStore();
    expect(useModelEditorStore.getState().isDirty).toBe(true);
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: /discard unsaved edits/i }),
    );

    await waitFor(() => expect(discardMock).toHaveBeenCalledTimes(1));
    expect(discardMock).toHaveBeenCalledWith(PROJECT_ID, MODEL_ID);
    // Behavioural: the editor state is reverted to the last saved baseline.
    await waitFor(() =>
      expect(useModelEditorStore.getState().isDirty).toBe(false),
    );
    // A draft discard does not move the save/deploy pointers.
    expect(useModelEditorStore.getState().lastSavedVersion).toBe(2);
    expect(useModelEditorStore.getState().deployedVersion).toBe(1);
  });

  it("discard-to-deployed reverts the store and calls Revert with the deployed version id", async () => {
    seedDirtyStore();
    renderPanel(true);

    // Wait for the versions list to resolve so the deployed row is known.
    const btn = await screen.findByRole("button", {
      name: /discard to deployed version/i,
    });
    await userEvent.click(btn);

    await waitFor(() => expect(revertMock).toHaveBeenCalledTimes(1));
    // discard-to-deployed reuses Revert, sending the version ID as the token.
    expect(revertMock).toHaveBeenCalledWith(
      PROJECT_ID,
      MODEL_ID,
      DEPLOYED_VERSION_ID,
      DEPLOYED_VERSION_ID,
    );
    await waitFor(() =>
      expect(useModelEditorStore.getState().isDirty).toBe(false),
    );
    // The governance-preserved note surfaces after discard-to-deployed.
    expect(await screen.findByText(/governance preserved/i)).toBeInTheDocument();
    // A draft discard must never be recorded as a new saved version.
    expect(discardMock).not.toHaveBeenCalled();
  });

  it("does not discard when the confirmation is dismissed", async () => {
    confirmMock.mockResolvedValue(false);
    seedDirtyStore();
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: /discard unsaved edits/i }),
    );
    await waitFor(() => expect(confirmMock).toHaveBeenCalledTimes(1));
    expect(discardMock).not.toHaveBeenCalled();
    // Store stays dirty because nothing was discarded.
    expect(useModelEditorStore.getState().isDirty).toBe(true);
  });

  it("hides discard-to-deployed for a caller who cannot revert", async () => {
    seedDirtyStore();
    renderPanel(false);
    await screen.findByText("Saved but not deployed");
    expect(
      screen.queryByRole("button", { name: /discard to deployed version/i }),
    ).not.toBeInTheDocument();
    // Discard unsaved edits (draft-only) is still available.
    expect(
      screen.getByRole("button", { name: /discard unsaved edits/i }),
    ).toBeInTheDocument();
  });

  it("shows a 'no pending changes' message when everything is in sync", async () => {
    pendingMock.mockReturnValue({
      isLoading: false,
      isError: false,
      data: {
        unsaved: {
          base_version: 2,
          base_version_unavailable: false,
          diff: {},
        },
        saved_undeployed: { deployed_version: 2, saved_version: 2, diff: {} },
      },
    });
    seedDirtyStore();
    renderPanel();
    expect(screen.getByText(/no pending changes/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /discard unsaved edits/i }),
    ).not.toBeInTheDocument();
  });
});
