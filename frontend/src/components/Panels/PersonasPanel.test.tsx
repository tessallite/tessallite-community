import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";
import { parseFilterRows, serializeFilterRows } from "./PersonasPanel";

const usePersonasMock = vi.fn();
const useMeasuresMock = vi.fn();
const useDimensionsMock = vi.fn();
const useHierarchiesMock = vi.fn();
const useDataTagsMock = vi.fn();
const createMock = vi.fn();
const updateMock = vi.fn();
const deleteMock = vi.fn();
const getRestrictionsMock = vi.fn();
const setRestrictionsMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  usePersonas: (...args: unknown[]) => usePersonasMock(...args),
  useMeasures: (...args: unknown[]) => useMeasuresMock(...args),
  useDimensions: (...args: unknown[]) => useDimensionsMock(...args),
  useHierarchies: (...args: unknown[]) => useHierarchiesMock(...args),
  useDataTags: (...args: unknown[]) => useDataTagsMock(...args),
}));

vi.mock("../../api/client", () => ({
  personasApi: {
    create: (...args: unknown[]) => createMock(...args),
    update: (...args: unknown[]) => updateMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  },
  dataTagsApi: {
    getPersonaRestrictions: (...args: unknown[]) => getRestrictionsMock(...args),
    setPersonaRestrictions: (...args: unknown[]) => setRestrictionsMock(...args),
  },
}));

import PersonasPanel from "./PersonasPanel";

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
          <Routes>
            <Route
              path="/p/:projectId/m/:modelId"
              element={<PersonasPanel />}
            />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

describe("PersonasPanel", () => {
  // Bug-6677: MUI Dialog/Select transitions fire real setTimeout callbacks at
  // unpredictable times under full-suite CPU contention. This causes async
  // setState calls to leak across test boundaries, producing intermittent
  // failures (pass in isolation, fail under parallel vitest). Fake timers give
  // deterministic control; afterEach flushes remaining timers before React
  // cleanup to prevent setState-after-unmount.
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    usePersonasMock.mockReset();
    useMeasuresMock.mockReset();
    useDimensionsMock.mockReset();
    useHierarchiesMock.mockReset();
    useDataTagsMock.mockReset();
    createMock.mockReset();
    updateMock.mockReset();
    deleteMock.mockReset();
    getRestrictionsMock.mockReset();
    setRestrictionsMock.mockReset();
    getRestrictionsMock.mockResolvedValue([]);
    setRestrictionsMock.mockResolvedValue([]);

    useMeasuresMock.mockReturnValue({ data: [], isLoading: false });
    useDimensionsMock.mockReturnValue({ data: [], isLoading: false });
    useHierarchiesMock.mockReturnValue({ data: [], isLoading: false });
    useDataTagsMock.mockReturnValue({ data: [], isLoading: false });
    // F-026-04: authoring controls now require an editor role (and not
    // read-only). These tests exercise the modeller authoring path.
    window.localStorage.setItem("user_role", "modeler");
  });

  afterEach(() => {
    act(() => { vi.runOnlyPendingTimers(); });
    cleanup();
    vi.useRealTimers();
    window.localStorage.removeItem("user_role");
  });

  it("shows empty-state when there are no personas", () => {
    usePersonasMock.mockReturnValue({ data: [], isLoading: false });

    renderPanel();

    expect(
      screen.getByText(/no personas yet/i),
    ).toBeInTheDocument();
  });

  it("creates a persona via the API on Save", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({ data: [], isLoading: false });
    createMock.mockResolvedValue({
      id: "new-id",
      model_id: "model-1",
      slug: "sales",
      name: "Sales",
      description: null,
      included_measure_ids: [],
      included_dimension_ids: [],
      included_hierarchy_ids: [],
      audience_roles: [],
      default_filters: {},
      includes_hidden_columns: false,
      created_at: "2026-04-24T00:00:00Z",
      updated_at: "2026-04-24T00:00:00Z",
    });

    renderPanel();

    await user.click(screen.getByRole("button", { name: /new persona/i }));
    await user.type(screen.getByLabelText(/^name/i), "Sales");
    await user.type(screen.getByLabelText(/^slug/i), "sales");
    await user.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() => {
      expect(createMock).toHaveBeenCalledTimes(1);
    });
    expect(createMock).toHaveBeenCalledWith(
      "proj-1",
      "model-1",
      expect.objectContaining({ name: "Sales", slug: "sales" }),
    );
  });

  it("renders existing personas in the table", () => {
    usePersonasMock.mockReturnValue({
      data: [
        {
          id: "p1",
          model_id: "model-1",
          slug: "finance",
          name: "Finance scope",
          description: "Finance analyst view",
          included_measure_ids: ["m1"],
          included_dimension_ids: [],
          included_hierarchy_ids: [],
          audience_roles: ["finance_analyst"],
          default_filters: {},
          bypass_row_security: false,
          includes_hidden_columns: false,
          created_at: "2026-04-24T00:00:00Z",
          updated_at: "2026-04-24T00:00:00Z",
        },
      ],
      isLoading: false,
    });

    renderPanel();

    expect(screen.getByText("Finance scope")).toBeInTheDocument();
    expect(screen.getByText("finance_analyst")).toBeInTheDocument();
  });

  it("requires confirmation before enabling bypass_row_security on save", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({ data: [], isLoading: false });
    createMock.mockResolvedValue({
      id: "new-id",
      model_id: "model-1",
      slug: "bypass_scope",
      name: "Bypass scope",
      description: null,
      included_measure_ids: [],
      included_dimension_ids: [],
      included_hierarchy_ids: [],
      audience_roles: [],
      default_filters: {},
      bypass_row_security: true,
      includes_hidden_columns: false,
      created_at: "2026-04-24T00:00:00Z",
      updated_at: "2026-04-24T00:00:00Z",
    });

    renderPanel();

    await user.click(screen.getByRole("button", { name: /new persona/i }));
    await user.type(screen.getByLabelText(/^name/i), "Bypass scope");
    await user.type(screen.getByLabelText(/^slug/i), "bypass_scope");
    await user.click(
      screen.getByLabelText(/skip all enabled row-security rules/i),
    );
    await user.click(screen.getByRole("button", { name: /^create$/i }));

    // Dialog must appear before the create call is made
    expect(
      await screen.findByText(/Bypass Row Security\?/i),
    ).toBeInTheDocument();
    expect(createMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /confirm bypass/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    expect(createMock).toHaveBeenCalledWith(
      "proj-1",
      "model-1",
      expect.objectContaining({
        name: "Bypass scope",
        bypass_row_security: true,
      }),
    );
  });

  it("does not re-prompt when saving a persona that was already in bypass mode", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({
      data: [
        {
          id: "p1",
          model_id: "model-1",
          slug: "already_bypassed",
          name: "Already bypassed",
          description: null,
          included_measure_ids: [],
          included_dimension_ids: [],
          included_hierarchy_ids: [],
          audience_roles: [],
          default_filters: {},
          bypass_row_security: true,
          includes_hidden_columns: false,
          created_at: "2026-04-24T00:00:00Z",
          updated_at: "2026-04-24T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    updateMock.mockResolvedValue({});

    renderPanel();

    await user.click(screen.getByRole("button", { name: /edit/i }));
    const desc = screen.getByLabelText(/description/i);
    await user.type(desc, "new note");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    // No confirmation dialog appears — only the previously-visible Delete
    // destructive confirm-dialog could have been summoned, and we never
    // clicked delete. The update body still carries bypass=true.
    expect(updateMock).toHaveBeenCalledWith(
      "proj-1",
      "model-1",
      "p1",
      expect.objectContaining({ bypass_row_security: true }),
    );
  });
});

describe("PersonasPanel — column restriction persistence (F-008-07)", () => {
  const TAG = {
    id: "tag-1",
    model_id: "model-1",
    tag_name: "PII",
    description: null,
    created_at: "2026-04-24T00:00:00Z",
    columns: [
      { column_id: "c1", table_name: "customers", column_name: "email" },
    ],
  };
  const PERSONA = {
    id: "p1",
    model_id: "model-1",
    slug: "partner",
    name: "Partner",
    description: null,
    included_measure_ids: [],
    included_dimension_ids: [],
    included_hierarchy_ids: [],
    audience_roles: [],
    default_filters: {},
    bypass_row_security: false,
    includes_hidden_columns: false,
    created_at: "2026-04-24T00:00:00Z",
    updated_at: "2026-04-24T00:00:00Z",
  };

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    usePersonasMock.mockReset();
    createMock.mockReset();
    updateMock.mockReset();
    deleteMock.mockReset();
    getRestrictionsMock.mockReset();
    setRestrictionsMock.mockReset();
    getRestrictionsMock.mockResolvedValue([]);
    setRestrictionsMock.mockResolvedValue([]);
    useMeasuresMock.mockReturnValue({ data: [], isLoading: false });
    useDimensionsMock.mockReturnValue({ data: [], isLoading: false });
    useHierarchiesMock.mockReturnValue({ data: [], isLoading: false });
    useDataTagsMock.mockReturnValue({ data: [TAG], isLoading: false });
    // F-026-04: authoring controls now require an editor role.
    window.localStorage.setItem("user_role", "modeler");
  });

  afterEach(() => {
    act(() => { vi.runOnlyPendingTimers(); });
    cleanup();
    vi.useRealTimers();
    window.localStorage.removeItem("user_role");
  });

  it("persists ticked restrictions atomically when creating a persona (Bug-7051)", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({ data: [], isLoading: false });
    createMock.mockResolvedValue({ ...PERSONA, id: "new-id" });

    renderPanel();

    await user.click(screen.getByRole("button", { name: /new persona/i }));
    await user.type(screen.getByLabelText(/^name/i), "Partner");
    await user.type(screen.getByLabelText(/^slug/i), "partner");
    await user.click(screen.getByRole("checkbox", { name: /PII/ }));
    await user.click(screen.getByRole("button", { name: /^create$/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    // Bug-7051: restricted_tag_ids is sent in the single atomic payload,
    // NOT via a separate setPersonaRestrictions call.
    expect(createMock).toHaveBeenCalledWith(
      "proj-1",
      "model-1",
      expect.objectContaining({ restricted_tag_ids: ["tag-1"] }),
    );
    expect(setRestrictionsMock).not.toHaveBeenCalled();
  });

  it("surfaces a backend error when the atomic save fails on update (Bug-7051)", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({ data: [PERSONA], isLoading: false });
    // Bug-7051: the backend validates restricted_tag_ids atomically and
    // returns an error in the same response — no separate restriction call.
    updateMock.mockRejectedValue(new Error("Invalid tag IDs"));

    renderPanel();

    await user.click(screen.getByRole("button", { name: /edit/i }));
    await user.click(await screen.findByRole("checkbox", { name: /PII/ }));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    expect(
      await screen.findByText(/Invalid tag IDs/i),
    ).toBeInTheDocument();
    expect(setRestrictionsMock).not.toHaveBeenCalled();
  });

  it("locks the restrictions section when loading current restrictions fails", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({ data: [PERSONA], isLoading: false });
    getRestrictionsMock.mockRejectedValue(new Error("boom"));
    updateMock.mockResolvedValue({});

    renderPanel();

    await user.click(screen.getByRole("button", { name: /edit/i }));
    expect(
      await screen.findByText(/could not load this persona's column restrictions/i),
    ).toBeInTheDocument();
    const checkbox = await screen.findByRole("checkbox", { name: /PII/ });
    expect(checkbox).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    // Bug-7051: when restrictions could not be loaded, the body must NOT
    // include restricted_tag_ids — otherwise the backend would silently
    // wipe the persona's column security.
    const updateBody = updateMock.mock.calls[0][3];
    expect(updateBody).not.toHaveProperty("restricted_tag_ids");
  });
});

describe("PersonasPanel empty-audience warning (F-008-12)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    usePersonasMock.mockReset();
    useMeasuresMock.mockReset();
    useDimensionsMock.mockReset();
    useHierarchiesMock.mockReset();
    useDataTagsMock.mockReset();
    getRestrictionsMock.mockReset();
    getRestrictionsMock.mockResolvedValue([]);
    useMeasuresMock.mockReturnValue({
      data: [{ id: "m1", name: "Revenue" }],
      isLoading: false,
    });
    useDimensionsMock.mockReturnValue({ data: [], isLoading: false });
    useHierarchiesMock.mockReturnValue({ data: [], isLoading: false });
    useDataTagsMock.mockReturnValue({ data: [], isLoading: false });
    window.localStorage.setItem("user_role", "modeler");
  });

  afterEach(() => {
    act(() => { vi.runOnlyPendingTimers(); });
    cleanup();
    vi.useRealTimers();
    window.localStorage.removeItem("user_role");
  });

  it("warns when a narrowing persona has no audience role", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({
      data: [{
        id: "p-sales",
        model_id: "model-1",
        slug: "sales",
        name: "Sales",
        description: null,
        included_measure_ids: ["m1"],
        included_dimension_ids: [],
        included_hierarchy_ids: [],
        audience_roles: [],
        default_filters: {},
        bypass_row_security: false,
        includes_hidden_columns: false,
        created_at: "2026-04-24T00:00:00Z",
        updated_at: "2026-04-24T00:00:00Z",
      }],
      isLoading: false,
    });

    renderPanel();
    await user.click(screen.getByRole("button", { name: /edit/i }));
    expect(
      await screen.findByText(/empty audience would apply it to everyone/i),
    ).toBeInTheDocument();
  });

  it("offers a structured default-filters editor (G-008-02)", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({
      data: [{
        id: "p-sales",
        model_id: "model-1",
        slug: "sales",
        name: "Sales",
        description: null,
        included_measure_ids: ["m1"],
        included_dimension_ids: [],
        included_hierarchy_ids: [],
        audience_roles: ["sales"],
        default_filters: {},
        bypass_row_security: false,
        includes_hidden_columns: false,
        created_at: "2026-04-24T00:00:00Z",
        updated_at: "2026-04-24T00:00:00Z",
      }],
      isLoading: false,
    });
    useDimensionsMock.mockReturnValue({
      data: [{ id: "d1", name: "region", display_name: "Region" }],
      isLoading: false,
    });

    renderPanel();
    await user.click(screen.getByRole("button", { name: /edit/i }));
    expect(await screen.findByRole("button", { name: /add filter/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/advanced json/i)).toBeInTheDocument();
  });
});

describe("default_filters serialize/parse round-trip (INTEG-09)", () => {
  it("preserves the not_in operator across a reload", () => {
    // not_in previously serialised to a bare array, which parseFilterRows reads
    // back as `in` — silently losing the operator. It must survive the round trip.
    const rows = [{ dim: "region", op: "not_in", value: "EMEA, APAC" }];
    const json = serializeFilterRows(rows);
    // Typed object form (the shape the backend maps to not_in).
    expect(JSON.parse(json)).toEqual({ region: { not_in: ["EMEA", "APAC"] } });
    const back = parseFilterRows(json);
    expect(back).toEqual([{ dim: "region", op: "not_in", value: "EMEA, APAC" }]);
  });

  it("keeps in as a bare array and round-trips it", () => {
    const rows = [{ dim: "region", op: "in", value: "EMEA, APAC" }];
    const json = serializeFilterRows(rows);
    expect(JSON.parse(json)).toEqual({ region: ["EMEA", "APAC"] });
    expect(parseFilterRows(json)).toEqual([
      { dim: "region", op: "in", value: "EMEA, APAC" },
    ]);
  });
});
