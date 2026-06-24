import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

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
  beforeEach(() => {
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
  });

  it("persists ticked restrictions when creating a persona", async () => {
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
    await waitFor(() =>
      expect(setRestrictionsMock).toHaveBeenCalledWith(
        "proj-1",
        "model-1",
        "new-id",
        { tag_ids: ["tag-1"] },
      ),
    );
  });

  it("surfaces an error when the restriction save fails on update", async () => {
    const user = userEvent.setup();
    usePersonasMock.mockReturnValue({ data: [PERSONA], isLoading: false });
    updateMock.mockResolvedValue({});
    setRestrictionsMock.mockRejectedValue(new Error("boom"));

    renderPanel();

    await user.click(screen.getByRole("button", { name: /edit/i }));
    await user.click(await screen.findByRole("checkbox", { name: /PII/ }));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(setRestrictionsMock).toHaveBeenCalled());
    expect(
      await screen.findByText(/column restrictions could not be saved/i),
    ).toBeInTheDocument();
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
    // The unloaded restriction set must never be written back.
    expect(setRestrictionsMock).not.toHaveBeenCalled();
  });
});
