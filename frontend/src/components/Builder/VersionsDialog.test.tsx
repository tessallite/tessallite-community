import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import VersionsDialog from "./VersionsDialog";

// Bug-6201 / F-013-01: Revert must send the language-neutral version ID to the
// backend, not the localized typed-confirm phrase (versions.py only accepts the
// version UUID or the English "revert to v{N}" phrase). Sending the localized
// phrase 400s on every non-English locale, making rollback unusable there.

const revertMock = vi.fn().mockResolvedValue({ status: "ok" });
const listMock = vi.fn();

vi.mock("../../api/versionsApi", () => ({
  versionsApi: {
    list: (...args: unknown[]) => listMock(...args),
    revert: (...args: unknown[]) => revertMock(...args),
    deploy: vi.fn(),
  },
  isSingletonDiff: () => false,
}));

// Bug-7616: Revert is gated on isTenantAdmin() (backend require_role("admin")),
// which reads user_role from localStorage. Default the tests to a tenant admin
// so the Revert button renders; the gating test overrides this.
function setRole(role: string) {
  localStorage.setItem("user_role", role);
}

// VersionDiffPanel is imported at module load but only rendered on the diff
// tab; stub it so the test doesn't pull its dependency tree.
vi.mock("./VersionDiffPanel", () => ({ default: () => null }));

// G-013-02: revert is offered to a PROJECT-scoped admin via the server-derived
// caller_can_admin on the model-detail response. Mock useModel so the test can
// set that flag without a real query.
let callerCanAdmin: boolean | null = null;
vi.mock("../../api/hooks", () => ({
  useModel: () => ({ data: { caller_can_admin: callerCanAdmin } }),
}));

const confirmMock = vi.fn().mockResolvedValue(true);
vi.mock("../Confirm", () => ({
  useConfirm: () => confirmMock,
}));

vi.mock("../../store/useModelEditorStore", () => ({
  useModelEditorStore: (selector: (s: { markClean: () => void }) => unknown) =>
    selector({ markClean: vi.fn() }),
}));

const PROJECT_ID = "proj-1";
const MODEL_ID = "model-1";
const VERSION_ID = "11111111-2222-3333-4444-555555555555";

function renderDialog() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  const result = render(
    <QueryClientProvider client={qc}>
      <VersionsDialog
        open
        onClose={() => {}}
        projectId={PROJECT_ID}
        modelId={MODEL_ID}
      />
    </QueryClientProvider>,
  );
  return { ...result, invalidateSpy };
}

/** All query keys invalidated by the revert success handler this run. */
function invalidatedKeys(spy: ReturnType<typeof vi.spyOn>): unknown[][] {
  return spy.mock.calls
    .map((c) => (c[0] as { queryKey?: unknown[] } | undefined)?.queryKey)
    .filter((k): k is unknown[] => Array.isArray(k));
}

describe("VersionsDialog revert (Bug-6201)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    callerCanAdmin = null;
    // Revert is admin-gated (Bug-7616); default these tests to an admin.
    setRole("tenant_admin");
    confirmMock.mockResolvedValue(true);
    revertMock.mockResolvedValue({ status: "ok" });
    listMock.mockResolvedValue([
      {
        id: VERSION_ID,
        version_number: 3,
        summary: "third",
        created_at: "2026-07-01T00:00:00Z",
        created_by: "someone@example.com",
        is_deployed: false,
      },
    ]);
  });

  it("sends the version ID to the API, not the localized confirm phrase", async () => {
    renderDialog();

    // Wait for the version row to render, then click its Revert button.
    const revertButton = await screen.findByText(/revert/i, {
      selector: "button",
    });
    await userEvent.click(revertButton);

    await waitFor(() => expect(revertMock).toHaveBeenCalledTimes(1));
    // versionsApi.revert(projectId, modelId, versionId, confirmText)
    expect(revertMock).toHaveBeenCalledWith(
      PROJECT_ID,
      MODEL_ID,
      VERSION_ID,
      VERSION_ID,
    );
  });

  it("does not revert when the confirmation is dismissed", async () => {
    confirmMock.mockResolvedValue(false);
    renderDialog();

    const revertButton = await screen.findByText(/revert/i, {
      selector: "button",
    });
    await userEvent.click(revertButton);

    // Give any pending microtasks a chance to run.
    await waitFor(() => expect(confirmMock).toHaveBeenCalledTimes(1));
    expect(revertMock).not.toHaveBeenCalled();
  });

  // Bug-7616: Revert is offered only to admins (backend require_role("admin")).
  it("hides the Revert button for a non-admin (modeler)", async () => {
    setRole("modeler");
    renderDialog();
    // The version row still renders (Deploy is present for modelers).
    await screen.findByText(/deploy/i, { selector: "button" });
    expect(
      screen.queryByRole("button", { name: /revert/i }),
    ).not.toBeInTheDocument();
  });

  // G-013-02: a PROJECT-scoped admin (not a tenant/system admin) may revert on
  // the backend; the button must be reachable for them via caller_can_admin.
  it("shows Revert for a project admin (caller_can_admin) who is not tenant admin", async () => {
    setRole("modeler"); // not a tenant/system admin
    callerCanAdmin = true; // but holds project admin for this model
    renderDialog();
    expect(
      await screen.findByRole("button", { name: /revert/i }),
    ).toBeInTheDocument();
  });

  // F-013-04: the governance-preserved note must be shown after a revert so an
  // admin knows security/certification were NOT rolled back.
  it("shows the governance-preserved note after a successful revert", async () => {
    revertMock.mockResolvedValue({
      status: "ok",
      governance_preserved: true,
      governance_preserved_note:
        "Governance preserved: personas, row security and data tags were not rolled back.",
    });
    renderDialog();
    const revertButton = await screen.findByText(/revert/i, {
      selector: "button",
    });
    await userEvent.click(revertButton);
    await waitFor(() =>
      expect(screen.getByText(/governance preserved/i)).toBeInTheDocument(),
    );
  });

  // Bug-7616: a Revert failure must surface an error, not vanish silently.
  it("surfaces an error when Revert fails", async () => {
    revertMock.mockRejectedValue({
      response: { data: { detail: "boom" } },
    });
    renderDialog();
    const revertButton = await screen.findByText(/revert/i, {
      selector: "button",
    });
    await userEvent.click(revertButton);
    await waitFor(() =>
      expect(screen.getByText(/revert failed/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/boom/i)).toBeInTheDocument();
  });

  // Bug-7152: revert must also invalidate the schema-v3 caches that live under
  // independent React Query keys (alias map, model settings, translations),
  // otherwise those panels show stale pre-revert data.
  it("invalidates alias-map, model-settings and translations caches on revert", async () => {
    const { invalidateSpy } = renderDialog();
    const revertButton = await screen.findByText(/revert/i, { selector: "button" });
    await userEvent.click(revertButton);
    await waitFor(() => expect(revertMock).toHaveBeenCalledTimes(1));

    await waitFor(() => {
      const keys = invalidatedKeys(invalidateSpy);
      const hasPrefix = (prefix: string) =>
        keys.some(
          (k) => k[0] === prefix && k[1] === PROJECT_ID && k[2] === MODEL_ID,
        );
      expect(hasPrefix("alias-map")).toBe(true);
      expect(hasPrefix("model-settings")).toBe(true);
      expect(hasPrefix("translations")).toBe(true);
    });
  });
});

// The history table row order: the version the gateway is currently serving
// must always sort first, then the rest by version_number descending. Deploy
// can point the runtime at ANY existing version, so version_number-desc order
// alone can bury a deployed older version below newer, undeployed drafts.
describe("VersionsDialog history table — deployed-first ordering", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    setRole("tenant_admin");
    confirmMock.mockResolvedValue(true);
  });

  function versionOrderInTable(): number[] {
    return screen
      .getAllByText(/^v\d+$/)
      .map((el) => Number(el.textContent!.slice(1)));
  }

  it("puts an older deployed version above newer, undeployed drafts", async () => {
    // Server order is plain version_number desc (v3, v2, v1); v2 is the one
    // currently deployed, and would otherwise render second, not first.
    listMock.mockResolvedValue([
      { id: "v3", version_number: 3, summary: "third", created_at: "2026-07-03T00:00:00Z", created_by: "a@x.com", is_deployed: false },
      { id: "v2", version_number: 2, summary: "second", created_at: "2026-07-02T00:00:00Z", created_by: "a@x.com", is_deployed: true },
      { id: "v1", version_number: 1, summary: "first", created_at: "2026-07-01T00:00:00Z", created_by: "a@x.com", is_deployed: false },
    ]);
    renderDialog();

    await waitFor(() => expect(versionOrderInTable()).toEqual([2, 3, 1]));
    // The deployed row still carries its "currently serving" marker.
    expect(screen.getByText("Currently serving")).toBeInTheDocument();
  });

  it("falls back to plain version_number-desc order when nothing is deployed", async () => {
    listMock.mockResolvedValue([
      { id: "v2", version_number: 2, summary: "second", created_at: "2026-07-02T00:00:00Z", created_by: "a@x.com", is_deployed: false },
      { id: "v1", version_number: 1, summary: "first", created_at: "2026-07-01T00:00:00Z", created_by: "a@x.com", is_deployed: false },
    ]);
    renderDialog();

    await waitFor(() => expect(versionOrderInTable()).toEqual([2, 1]));
  });
});
