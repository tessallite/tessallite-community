import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

const listSetsMock = vi.fn();
const createSetMock = vi.fn();
const updateSetMock = vi.fn();
const deleteSetMock = vi.fn();
const validateMock = vi.fn();
const previewMock = vi.fn();
const listDimsMock = vi.fn();
const versionsMock = vi.fn();
const revertMock = vi.fn();
const certifyMock = vi.fn();
const deprecateMock = vi.fn();

vi.mock("../../api/client", () => ({
  namedSetsApi: {
    list: (...args: unknown[]) => listSetsMock(...args),
    create: (...args: unknown[]) => createSetMock(...args),
    update: (...args: unknown[]) => updateSetMock(...args),
    delete: (...args: unknown[]) => deleteSetMock(...args),
    validate: (...args: unknown[]) => validateMock(...args),
    preview: (...args: unknown[]) => previewMock(...args),
    versions: (...args: unknown[]) => versionsMock(...args),
    revert: (...args: unknown[]) => revertMock(...args),
    certify: (...args: unknown[]) => certifyMock(...args),
    deprecate: (...args: unknown[]) => deprecateMock(...args),
    listUsage: vi.fn().mockResolvedValue([]),
    reportUsage: vi.fn().mockResolvedValue({}),
  },
  dimensionsApi: {
    list: (...args: unknown[]) => listDimsMock(...args),
  },
  preferencesApi: {
    get: vi.fn().mockResolvedValue({ favourites: { kpi: [], named_set: [] }, recently_used: { kpi: [], named_set: [] } }),
    toggleFavourite: vi.fn().mockResolvedValue({ favourited: true }),
    recordRecentlyUsed: vi.fn().mockResolvedValue({ recorded: true }),
  },
}));

vi.mock("../../auth/currentUser", () => ({
  canEditModelConfig: () => true,
  isTenantAdmin: () => true,
}));

import NamedSetsPanel from "./NamedSetsPanel";

const SAMPLE_DIMS = [
  { id: "d1", name: "Customer", display_name: "Customer" },
  { id: "d2", name: "Product", display_name: "Product" },
];

const SAMPLE_SETS = [
  {
    id: "ns1",
    name: "top_customers",
    display_name: "Top Customers",
    description: "Top 10 by revenue",
    display_folder: "Customer Lists",
    scope: 2,
    expression: "{TopCount([Customer].Members, 10, [Measures].[Revenue])}",
    dimensions: "[Customer]",
    list_type: "dynamic_top_n",
    certification_status: "certified",
    owner_user_id: null,
    builder_definition: {
      type: "topN",
      entity: "Customer",
      count: 10,
      measure: "Revenue",
      direction: "top",
    },
    created_at: "2026-01-01",
    updated_at: "2026-01-01",
  },
  {
    id: "ns2",
    name: "vip_payment",
    display_name: "VIP Payments",
    description: null,
    display_folder: null,
    scope: 1,
    expression: "{ [Payment].[Type].&[Credit], [Payment].[Type].&[Wire] }",
    dimensions: "[Payment]",
    list_type: "fixed",
    certification_status: "draft",
    owner_user_id: null,
    builder_definition: null,
    created_at: "2026-01-01",
    updated_at: "2026-01-01",
  },
];

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
              element={<NamedSetsPanel />}
            />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

describe("NamedSetsPanel", () => {
  beforeEach(() => {
    listSetsMock.mockReset();
    createSetMock.mockReset();
    updateSetMock.mockReset();
    deleteSetMock.mockReset();
    validateMock.mockReset();
    previewMock.mockReset();
    listDimsMock.mockReset();
    versionsMock.mockReset();
    revertMock.mockReset();
    certifyMock.mockReset();
    deprecateMock.mockReset();
  });

  it("renders heading", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue([]);
    renderPanel();
    expect(screen.getByText("Named Sets")).toBeTruthy();
  });

  it("shows empty state when no sets exist", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/No named sets defined/)).toBeTruthy();
    });
  });

  it("shows set cards with list_type and certification chips", async () => {
    listSetsMock.mockResolvedValue(SAMPLE_SETS);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Top Customers")).toBeTruthy();
      expect(screen.getByText("VIP Payments")).toBeTruthy();
    });
    expect(screen.getByText("Dynamic")).toBeTruthy();
    expect(screen.getByText("Fixed")).toBeTruthy();
    expect(screen.getByText("certified")).toBeTruthy();
  });

  it("opens tabbed create dialog with all 4 tabs", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    renderPanel();

    const user = userEvent.setup();
    await user.click(screen.getByText("Add Set"));

    await waitFor(() => {
      expect(
        screen.getByText("Add Named Set", { selector: "h2" }),
      ).toBeTruthy();
    });
    expect(screen.getByRole("tab", { name: "Basics" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "List Rule" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Scope & Governance" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Preview" })).toBeTruthy();
  });

  it("Preview tab is enabled for new sets (preview-by-definition)", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    renderPanel();

    const user = userEvent.setup();
    await user.click(screen.getByText("Add Set"));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "Preview" })).toBeTruthy();
    });
    const previewTab = screen.getByRole("tab", { name: "Preview" });
    expect(
      previewTab.classList.contains("Mui-disabled") ||
        previewTab.getAttribute("aria-disabled") === "true" ||
        (previewTab as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("switches to List Rule tab and shows list type selector", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    renderPanel();

    const user = userEvent.setup();
    await user.click(screen.getByText("Add Set"));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "List Rule" })).toBeTruthy();
    });
    await user.click(screen.getByRole("tab", { name: "List Rule" }));

    await waitFor(() => {
      expect(screen.getByText("Validate")).toBeTruthy();
    });
  });

  it("validation button calls validate endpoint and shows result", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    validateMock.mockResolvedValue({
      is_valid: true,
      errors: [],
      warnings: [],
      explanation: "Returns top 10 customers",
      estimated_cost_band: "low",
    });
    renderPanel();

    const user = userEvent.setup();
    await user.click(screen.getByText("Add Set"));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "List Rule" })).toBeTruthy();
    });
    await user.click(screen.getByRole("tab", { name: "List Rule" }));

    await waitFor(() => {
      expect(screen.getByText("Validate")).toBeTruthy();
    });
    await user.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(screen.getByText("Expression is valid.")).toBeTruthy();
      expect(screen.getByText("Returns top 10 customers")).toBeTruthy();
    });
  });

  it("validation shows errors for invalid expression", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    validateMock.mockResolvedValue({
      is_valid: false,
      errors: ["Blocked function: DRILLDOWNLEVEL"],
      warnings: [],
      explanation: null,
      estimated_cost_band: null,
    });
    renderPanel();

    const user = userEvent.setup();
    await user.click(screen.getByText("Add Set"));

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "List Rule" })).toBeTruthy();
    });
    await user.click(screen.getByRole("tab", { name: "List Rule" }));

    await waitFor(() => {
      expect(screen.getByText("Validate")).toBeTruthy();
    });
    await user.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(
        screen.getByText("Blocked function: DRILLDOWNLEVEL"),
      ).toBeTruthy();
    });
  });

  it("opens edit dialog with pre-filled data", async () => {
    listSetsMock.mockResolvedValue(SAMPLE_SETS);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    renderPanel();

    const user = userEvent.setup();
    await waitFor(() => {
      expect(screen.getByText("Top Customers")).toBeTruthy();
    });

    const editButtons = screen.getAllByText("Edit");
    await user.click(editButtons[0]);

    await waitFor(() => {
      expect(screen.getByText("Edit Named Set")).toBeTruthy();
    });
    const nameField = screen.getByLabelText("Name");
    expect((nameField as HTMLInputElement).value).toBe("top_customers");
    expect((nameField as HTMLInputElement).disabled).toBe(true);
  });

  it("shows scope & governance tab with certification dropdown", async () => {
    listSetsMock.mockResolvedValue([]);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    renderPanel();

    const user = userEvent.setup();
    await user.click(screen.getByText("Add Set"));

    await waitFor(() => {
      expect(
        screen.getByRole("tab", { name: "Scope & Governance" }),
      ).toBeTruthy();
    });
    await user.click(screen.getByRole("tab", { name: "Scope & Governance" }));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Global sets are visible to all sessions; session sets are per-connection.",
        ),
      ).toBeTruthy();
    });
  });

  it("shows History tab with version history in edit dialog", async () => {
    listSetsMock.mockResolvedValue(SAMPLE_SETS);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    versionsMock.mockResolvedValue([
      {
        id: "v1",
        version_number: 1,
        changed_by: "modeler@test.com",
        changed_at: "2026-05-19T12:00:00Z",
        change_summary: "Updated: expression",
        snapshot: {},
      },
    ]);
    renderPanel();

    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText("Top Customers")).toBeTruthy());

    const editButtons = screen.getAllByText("Edit");
    await user.click(editButtons[0]);
    await waitFor(() => expect(screen.getByRole("tab", { name: /History/ })).toBeTruthy());

    await user.click(screen.getByRole("tab", { name: /History/ }));
    await waitFor(() => {
      expect(screen.getByText("modeler@test.com")).toBeTruthy();
      expect(screen.getByText("Updated: expression")).toBeTruthy();
    });
  });

  it("shows Certify and Deprecate buttons for admin on History tab", async () => {
    const draftSets = [{ ...SAMPLE_SETS[1] }];
    listSetsMock.mockResolvedValue(draftSets);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    versionsMock.mockResolvedValue([]);
    renderPanel();

    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText("VIP Payments")).toBeTruthy());

    const editButtons = screen.getAllByText("Edit");
    await user.click(editButtons[0]);
    await waitFor(() => expect(screen.getByRole("tab", { name: /History/ })).toBeTruthy());
    await user.click(screen.getByRole("tab", { name: /History/ }));

    await waitFor(() => {
      expect(screen.getByText("Certify")).toBeTruthy();
      expect(screen.getByText("Deprecate")).toBeTruthy();
    });
  });

  it("shows deprecated warning banner on deprecated named set card", async () => {
    const deprecatedSets = [{ ...SAMPLE_SETS[0], certification_status: "deprecated" }];
    listSetsMock.mockResolvedValue(deprecatedSets);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText(/This named set is deprecated/)).toBeTruthy();
    });
  });

  it("History tab shows Revert button on version rows", async () => {
    listSetsMock.mockResolvedValue(SAMPLE_SETS);
    listDimsMock.mockResolvedValue(SAMPLE_DIMS);
    versionsMock.mockResolvedValue([
      {
        id: "v1",
        version_number: 1,
        changed_by: "admin@test.com",
        changed_at: "2026-05-20T14:00:00Z",
        change_summary: "Certified",
        snapshot: {},
      },
    ]);
    renderPanel();

    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByText("Top Customers")).toBeTruthy());

    const editButtons = screen.getAllByText("Edit");
    await user.click(editButtons[0]);
    await user.click(screen.getByRole("tab", { name: /History/ }));
    await waitFor(() => {
      expect(screen.getByText("Revert")).toBeTruthy();
    });
  });
});
