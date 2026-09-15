import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import HierarchiesPanel, { hierarchyLevelToCreate } from "./HierarchiesPanel";
import type { HierarchyLevel } from "../../api/types";

// Bug-8314: deleting a hierarchy must record enough undo data to restore the
// drill levels, not only the header. Producer half of the contract: the panel
// snapshots levels into the record's __levels key using hierarchyLevelToCreate;
// consumer half (replay via hierarchiesApi.createLevel) is covered in
// useCanvasHistory.test.tsx. The two halves meet on the HierarchyLevelCreate
// field names asserted here.

const authState = vi.hoisted(() => ({ canEdit: true }));
const health = vi.hoisted(() => vi.fn());
const recordDeleteMock = vi.hoisted(() => vi.fn());
const listLevelsMock = vi.hoisted(() => vi.fn());
const deleteHierarchyMock = vi.hoisted(() => vi.fn());

vi.mock("../../auth/useCanAuthorModel", () => ({
  useCanAuthorModel: () => authState.canEdit,
}));

vi.mock("../../api/hooks", () => ({
  useHierarchies: () => ({
    data: [
      {
        id: "hierarchy-1",
        name: "Geography",
        type: "explicit",
        level_names: ["Country", "City"],
      },
    ],
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
    listLevels: (...args: unknown[]) => listLevelsMock(...args),
    create: vi.fn(),
    update: vi.fn(),
    delete: (...args: unknown[]) => deleteHierarchyMock(...args),
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
  recordDelete: recordDeleteMock,
}));

vi.mock("../Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

const LEVELS: HierarchyLevel[] = [
  {
    id: "level-1",
    name: "Country",
    ordinal: 0,
    key_attribute: {
      id: "attr-1",
      name: "country_key",
      table_id: "t1",
      table_name: "T1",
      data_type: "string",
      source: "physical_column",
    },
    attributes: [
      {
        id: "la-1",
        attribute: {
          id: "attr-2",
          name: "country_name",
          table_id: "t1",
          table_name: "T1",
          data_type: "string",
          source: "physical_column",
        },
        role: "display",
      },
    ],
    description: "Country level",
    time_unit: null,
    allowed_time_calcs: [],
  },
  {
    id: "level-2",
    name: "City",
    ordinal: 1,
    key_attribute: {
      id: "attr-3",
      name: "city_key",
      table_id: "t1",
      table_name: "T1",
      data_type: "string",
      source: "physical_column",
    },
    attributes: [],
    description: null,
    time_unit: null,
    allowed_time_calcs: [],
  },
];

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

describe("HierarchiesPanel delete-undo level snapshot (Bug-8314)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    authState.canEdit = true;
    health.mockResolvedValue([]);
    listLevelsMock.mockResolvedValue(LEVELS);
    deleteHierarchyMock.mockResolvedValue(undefined);
  });

  it("maps a server level row to the HierarchyLevelCreate payload the replayer consumes", () => {
    expect(hierarchyLevelToCreate(LEVELS[0])).toEqual({
      name: "Country",
      ordinal: 0,
      key_attribute_id: "attr-1",
      key_attribute_source: "physical_column",
      description: "Country level",
      time_unit: null,
      allowed_time_calcs: [],
      attributes: [
        { attribute_id: "attr-2", attribute_source: "physical_column", role: "display" },
      ],
    });
    expect(hierarchyLevelToCreate(LEVELS[1])).toEqual({
      name: "City",
      ordinal: 1,
      key_attribute_id: "attr-3",
      key_attribute_source: "physical_column",
      description: undefined,
      time_unit: null,
      allowed_time_calcs: [],
      attributes: [],
    });
  });

  it("records the level snapshot in the delete-undo record alongside the header", async () => {
    renderPanel();

    // The health probe resolves empty; wait for the list row to be present.
    await screen.findByText("Geography");
    await userEvent.click(screen.getByTestId("hierarchy-delete-hierarchy-1"));

    await waitFor(() => expect(deleteHierarchyMock).toHaveBeenCalledTimes(1));
    // The snapshot is fetched BEFORE the delete, so the undo record carries it.
    expect(listLevelsMock).toHaveBeenCalledWith("project-1", "model-1", "hierarchy-1");
    expect(deleteHierarchyMock).toHaveBeenCalledWith("project-1", "model-1", "hierarchy-1");
    expect(recordDeleteMock).toHaveBeenCalledTimes(1);
    const [entity, id, data] = recordDeleteMock.mock.calls[0] as [string, string, Record<string, unknown>];
    expect(entity).toBe("hierarchy");
    expect(id).toBe("hierarchy-1");
    expect(data).toMatchObject({
      name: "Geography",
      type: "explicit",
    });
    expect(data.__levels).toEqual([
      {
        name: "Country",
        ordinal: 0,
        key_attribute_id: "attr-1",
        key_attribute_source: "physical_column",
        description: "Country level",
        time_unit: null,
        allowed_time_calcs: [],
        attributes: [
          { attribute_id: "attr-2", attribute_source: "physical_column", role: "display" },
        ],
      },
      {
        name: "City",
        ordinal: 1,
        key_attribute_id: "attr-3",
        key_attribute_source: "physical_column",
        description: undefined,
        time_unit: null,
        allowed_time_calcs: [],
        attributes: [],
      },
    ]);
  });

  it("fails closed when the level snapshot fails: no delete, no lossy undo (B1)", async () => {
    listLevelsMock.mockRejectedValue(new Error("network down"));
    renderPanel();

    await screen.findByText("Geography");
    await userEvent.click(screen.getByTestId("hierarchy-delete-hierarchy-1"));

    // B1 witness A: a failed snapshot must BLOCK the delete — proceeding would
    // record a header-only undo and make the drill levels permanently lost.
    await waitFor(() => expect(deleteHierarchyMock).not.toHaveBeenCalled());
    expect(recordDeleteMock).not.toHaveBeenCalled();
    // The failure is surfaced, not swallowed.
    expect(await screen.findByText(/network down/)).toBeTruthy();
  });
});
