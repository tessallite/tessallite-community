import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import HierarchiesPanel from "./HierarchiesPanel";

const authState = vi.hoisted(() => ({ canEdit: true }));
const health = vi.hoisted(() => vi.fn());

vi.mock("../../auth/useCanAuthorModel", () => ({
  useCanAuthorModel: () => authState.canEdit,
}));

vi.mock("../../api/hooks", () => ({
  useHierarchies: () => ({
    data: [{
      id: "hierarchy-1",
      name: "Geography",
      type: "explicit",
      level_names: ["Country", "City"],
    }],
    isLoading: false,
  }),
  useHierarchy: () => ({ data: null, isLoading: false }),
  useSources: () => ({ data: [], isLoading: false }),
  useAllModelTables: () => ({ data: [], isLoading: false }),
  useMeasures: () => ({ data: [], isLoading: false }),
  useTableAttributes: () => ({ data: [], isLoading: false }),
}));

vi.mock("../../api/client", () => ({
  hierarchiesApi: {
    health,
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    createLevel: vi.fn(),
    updateLevel: vi.fn(),
    deleteLevel: vi.fn(),
    reorderLevels: vi.fn(),
    generateDate: vi.fn(),
    generateSegment: vi.fn(),
    listUnassignedDates: vi.fn().mockResolvedValue([]),
    createDateIntelligence: vi.fn(),
    preview: vi.fn(),
  },
}));

vi.mock("../Builder/emitDrawerHistory", () => ({
  recordCreate: vi.fn(),
  recordUpdate: vi.fn(),
  recordDelete: vi.fn(),
}));

vi.mock("../Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/projects/project-1/models/model-1"]}>
      <QueryClientProvider client={queryClient}>
        <I18nContext.Provider value={en}>
          <Routes>
            <Route path="/projects/:projectId/models/:modelId" element={<HierarchiesPanel />} />
          </Routes>
        </I18nContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("HierarchiesPanel member-integrity diagnostics (Bug-8291)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authState.canEdit = true;
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "error",
      members_probed: true,
      issues: [
        {
          issue_type: "member_orphan_children",
          severity: "error",
          detail: {
            parent_level: "Country",
            child_level: "City",
            reason: "Two cities have no country parent.",
            sample_keys: ["Lost City", "Nowhere"],
            sampled: 2,
            truncated: false,
          },
        },
        {
          issue_type: "member_multiple_parents",
          severity: "error",
          detail: {
            parent_level: "Country",
            child_level: "City",
            reason: "Springfield maps to multiple countries.",
            sample_keys: ["Springfield"],
            sampled: 1,
            truncated: true,
          },
        },
      ],
    }]);
  });

  it("requests the bounded modeler probe and visibly renders orphan and many-to-many bodies", async () => {
    renderPanel();
    await waitFor(() => expect(health).toHaveBeenCalledWith("project-1", "model-1", true));
    expect(await screen.findByRole("img", { name: "Hierarchy health: error" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Health diagnostics for Geography" })).toBeInTheDocument();
    expect(screen.getByText("Orphan child members")).toBeInTheDocument();
    expect(screen.getByText("Members with multiple parents")).toBeInTheDocument();
    expect(screen.getAllByText("Parent level: Country. Child level: City.")).toHaveLength(2);
    expect(screen.getByText("Two cities have no country parent.")).toBeInTheDocument();
    expect(screen.getByText("Sample member keys: Lost City, Nowhere.")).toBeInTheDocument();
    expect(screen.getByText(/more affected members may exist/i)).toBeInTheDocument();
  });

  it("marks a viewer's metadata-only result as unchecked, never as healthy", async () => {
    authState.canEdit = false;
    // EXACT real backend shape for probe_members=false: the member probe never
    // runs and the response says NOTHING about member integrity. Rendering this
    // as "healthy" is the Bug-8291 false-healthy defect.
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "ok",
      members_probed: false,
      issues: [],
    }]);
    renderPanel();
    await waitFor(() => expect(health).toHaveBeenCalledWith("project-1", "model-1", false));
    expect(
      await screen.findByRole("img", {
        name: "Hierarchy health: metadata checks passed, member integrity not checked",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Hierarchy health: healthy" })).toBeNull();
    expect(screen.getByTestId("member-probe-not-run")).toBeInTheDocument();
    expect(screen.getByText("Member integrity was not checked")).toBeInTheDocument();
    expect(
      screen.getByText(/Member-integrity probes read source data and run only for modellers/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Orphan child members")).toBeNull();
  });

  it("reports the probe as unchecked when the model binding rejects a global modeler's probe", async () => {
    // EXACT real fallback shape: the retry is metadata-only, so the backend
    // returns a clean `ok` with an EMPTY issue list. Nothing in the payload
    // records that the probe was refused — the panel must carry that state.
    health
      .mockRejectedValueOnce({ response: { status: 403 } })
      .mockResolvedValueOnce([{
        hierarchy_id: "hierarchy-1",
        hierarchy_name: "Geography",
        status: "ok",
        members_probed: false,
        issues: [],
      }]);

    renderPanel();

    await waitFor(() => expect(health).toHaveBeenNthCalledWith(1, "project-1", "model-1", true));
    await waitFor(() => expect(health).toHaveBeenNthCalledWith(2, "project-1", "model-1", false));
    expect(
      await screen.findByRole("img", {
        name: "Hierarchy health: metadata checks passed, member integrity not checked",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Hierarchy health: healthy" })).toBeNull();
    expect(screen.getByText("Member integrity was not checked")).toBeInTheDocument();
    expect(
      screen.getByText(/Your access to this model does not permit member-integrity probes/),
    ).toBeInTheDocument();
    // The rejection is also stated once at panel level.
    expect(
      screen.getByText(/Member-integrity probes were rejected for your access to this model/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sample member keys: Lost City, Nowhere.")).toBeNull();
  });

  it("reports healthy only when the member probe actually ran", async () => {
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "ok",
      members_probed: true,
      issues: [],
    }]);
    renderPanel();
    await waitFor(() => expect(health).toHaveBeenCalledWith("project-1", "model-1", true));
    expect(await screen.findByRole("img", { name: "Hierarchy health: healthy" })).toBeInTheDocument();
    expect(screen.queryByTestId("member-probe-not-run")).toBeNull();
  });

  it("keeps a real backend error visible on the metadata-only fallback path", async () => {
    // A `denied` fallback must not downgrade or mask a genuine metadata error.
    health
      .mockRejectedValueOnce({ response: { status: 403 } })
      .mockResolvedValueOnce([{
        hierarchy_id: "hierarchy-1",
        hierarchy_name: "Geography",
        status: "error",
        members_probed: false,
        issues: [{ issue_type: "empty_levels", severity: "error", detail: {} }],
      }]);

    renderPanel();

    expect(await screen.findByRole("img", { name: "Hierarchy health: error" })).toBeInTheDocument();
    // ...and the unchecked-probe notice is still present alongside it.
    expect(screen.getByTestId("member-probe-not-run")).toBeInTheDocument();
  });

  // Finding R3-1: probe ran but scanned ZERO pairs. Real backend shape for a
  // hierarchy whose adjacent levels resolve to different source tables (or
  // UDAs): probe_members=true, only info-severity member_integrity_unprobed
  // issues, aggregate status stays "ok". The status indicator must not claim
  // full health while the diagnostics say the members were not checked.
  it("does not announce healthy when the probe ran but scanned zero level pairs", async () => {
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "ok",
      members_probed: false,
      issues: [{
        issue_type: "member_integrity_unprobed",
        severity: "info",
        detail: {
          parent_level: "Country",
          child_level: "City",
          reason: "Parent and child levels resolve to different source tables; cross-table member integrity is not scanned yet.",
        },
      }],
    }]);
    renderPanel();
    await waitFor(() => expect(health).toHaveBeenCalledWith("project-1", "model-1", true));
    expect(await screen.findByText("Member integrity was not checked")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Hierarchy health: healthy" })).toBeNull();
    expect(
      screen.getByRole("img", {
        name: "Hierarchy health: metadata checks passed, member integrity coverage not confirmed",
      }),
    ).toBeInTheDocument();
  });

  // Finding R3-2 (a11y): an ARIA button makes its subtree presentational, so
  // the diagnostics must never be rendered inside the selectable row button —
  // that flattens every issue into one run-on accessible name.
  it("renders the diagnostics outside the selectable row button", async () => {
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "error",
      members_probed: true,
      issues: [{
        issue_type: "member_orphan_children",
        severity: "error",
        detail: { parent_level: "Country", child_level: "City", reason: "Two cities have no country parent." },
      }],
    }]);
    renderPanel();
    const diagnostics = await screen.findByRole("list", { name: "Health diagnostics for Geography" });
    const rowButton = screen.getByRole("button", { name: /Geography/ });
    expect(rowButton.contains(diagnostics)).toBe(false);
  });

  // Finding R4-2: the ten METADATA issue types fell through to the raw-token
  // fallback, so a broken level chain read "Hierarchy issue:
  // dangling_key_attribute" with no detail at all.
  it("renders translated titles and detail for metadata issue types", async () => {
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "error",
      members_probed: true,
      issues: [
        {
          issue_type: "dangling_key_attribute",
          severity: "error",
          detail: { level_name: "City", ordinal: 1, key_attribute_source: "physical_column" },
        },
        { issue_type: "duplicate_level_ordinals", severity: "error", detail: { ordinals: [0, 0, 1] } },
        {
          issue_type: "calendar_type_mismatch",
          severity: "warning",
          detail: { expected_type: "retail_445", actual_type: "standard" },
        },
      ],
    }]);
    renderPanel();

    expect(await screen.findByText("Level key attribute no longer exists")).toBeInTheDocument();
    // Bug-8291 R1 review: the level ORDINAL the backend sends alongside the
    // name was being dropped. Two levels in one hierarchy may share a name, so
    // the position is what pins the diagnostic to exactly one level.
    expect(screen.getByText("Level: City (position 1).")).toBeInTheDocument();
    expect(screen.getByText("Key attribute source: physical_column.")).toBeInTheDocument();
    expect(screen.getByText("Two or more levels share the same position")).toBeInTheDocument();
    expect(screen.getByText("Level positions: 0, 0, 1.")).toBeInTheDocument();
    expect(
      screen.getByText("The joined calendar table has a different calendar type"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Expected calendar type retail_445; the joined calendar table is standard."),
    ).toBeInTheDocument();
    // No raw snake_case token leaks through.
    expect(screen.queryByText(/Hierarchy issue: /)).toBeNull();
  });

  // Bug-8512: a hierarchy mutation changes the health verdict, so the health
  // query must be invalidated with it. Without this the panel kept rendering
  // the pre-edit diagnostics until it was remounted.
  it("refetches health after a hierarchy mutation", async () => {
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "ok",
      members_probed: true,
      issues: [],
    }]);
    renderPanel();
    await waitFor(() => expect(health).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByTestId("hierarchy-delete-hierarchy-1"));

    await waitFor(() => expect(health).toHaveBeenCalledTimes(2));
  });

  it("does not retry as metadata-only on a non-403 failure", async () => {
    health.mockRejectedValue({ response: { status: 500 } });
    renderPanel();
    await waitFor(() =>
      expect(
        screen.getByText(/Hierarchy health diagnostics could not be loaded/),
      ).toBeInTheDocument(),
    );
    expect(health).toHaveBeenCalledTimes(1);
    expect(health).toHaveBeenCalledWith("project-1", "model-1", true);
  });

  it("visibly distinguishes unprobed and failed member-integrity checks", async () => {
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "warning",
      members_probed: false,
      issues: [
        {
          issue_type: "member_integrity_unprobed",
          severity: "info",
          detail: {
            parent_level: "Country",
            child_level: "City",
            reason: "The levels resolve to different source tables.",
          },
        },
        {
          issue_type: "member_integrity_probe_failed",
          severity: "warning",
          detail: {
            parent_level: "Country",
            child_level: "City",
            check: "orphan_children",
            error: "SourceTimeout",
          },
        },
      ],
    }]);

    renderPanel();

    expect(await screen.findByText("Member integrity was not checked")).toBeInTheDocument();
    expect(screen.getByText("Member-integrity check failed")).toBeInTheDocument();
    expect(screen.getByText("The levels resolve to different source tables.")).toBeInTheDocument();
    expect(screen.getByText("Check: orphan_children. Error: SourceTimeout.")).toBeInTheDocument();
  });

  // Bug-8510: the API now STATES coverage per hierarchy. The panel must obey
  // that statement instead of reconstructing it. This shape — probe requested,
  // status "ok", NO issues at all, coverage false — is unreachable for the
  // client-side reconstruction the panel used to do (it looked for
  // member_integrity_unprobed in the issue list), so it fails the moment the
  // authoritative field stops being read.
  it("obeys the API when it reports the members were not fully probed", async () => {
    health.mockResolvedValue([{
      hierarchy_id: "hierarchy-1",
      hierarchy_name: "Geography",
      status: "ok",
      members_probed: false,
      issues: [],
    }]);
    renderPanel();
    await waitFor(() => expect(health).toHaveBeenCalledWith("project-1", "model-1", true));
    expect(
      await screen.findByRole("img", {
        name: "Hierarchy health: metadata checks passed, member integrity coverage not confirmed",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Hierarchy health: healthy" })).toBeNull();
    // R1-2: a partial verdict must never be left unexplained. The backend named
    // no skipped pair here, so the generic coverage notice stands in.
    expect(screen.getByTestId("member-probe-incomplete")).toBeInTheDocument();
    expect(screen.getByText("Member integrity coverage was not confirmed")).toBeInTheDocument();
    expect(
      screen.getByText(/did not confirm that the member-integrity checks covered every level/),
    ).toBeInTheDocument();
    // ...and it must not misreport a modeller's own probe as never requested.
    expect(
      screen.queryByText(/run only for modellers who can edit this model/),
    ).toBeNull();
  });

  // Fail closed on a backend that predates the field (mixed-version deploy):
  // "the server did not say" must never render as "checked and healthy".
  it("does not claim health when the response omits the coverage field", async () => {
    health.mockResolvedValue([
      { hierarchy_id: "hierarchy-1", hierarchy_name: "Geography", status: "ok", issues: [] },
    ]);
    renderPanel();
    await waitFor(() => expect(health).toHaveBeenCalledWith("project-1", "model-1", true));
    expect(
      await screen.findByRole("img", {
        name: "Hierarchy health: metadata checks passed, member integrity coverage not confirmed",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Hierarchy health: healthy" })).toBeNull();
  });
});
