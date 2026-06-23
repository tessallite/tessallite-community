import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TableEditDialog from "./index";
import type { ModelTable, TableAttribute } from "../../../api/types";

const mockUpdateTable = vi.fn();
const mockUpdateColumn = vi.fn();
const mockCreateUda = vi.fn();
const mockListAttrs = vi.fn();
const mockListUda = vi.fn();
const mockFunctionCatalog = vi.fn();

const mockListMeasures = vi.fn();
const mockListDimensions = vi.fn();

vi.mock("../../../api/client", () => ({
  modelTablesApi: {
    update: (...args: unknown[]) => mockUpdateTable(...args),
    analyze: vi.fn(),
  },
  tableAttributesApi: {
    list: (...args: unknown[]) => mockListAttrs(...args),
    updateColumn: (...args: unknown[]) => mockUpdateColumn(...args),
    delete: vi.fn(),
    syncColumns: vi.fn(),
  },
  userDefinedAttributesApi: {
    list: (...args: unknown[]) => mockListUda(...args),
    create: (...args: unknown[]) => mockCreateUda(...args),
    update: vi.fn(),
    validate: vi.fn(),
    functionCatalog: (...args: unknown[]) => mockFunctionCatalog(...args),
  },
  connectionsApi: {
    discoverColumns: vi.fn(),
  },
  measuresApi: {
    list: (...args: unknown[]) => mockListMeasures(...args),
    create: vi.fn(),
  },
  dimensionsApi: {
    list: (...args: unknown[]) => mockListDimensions(...args),
    create: vi.fn(),
  },
}));

const fakeTable: ModelTable = {
  id: "tbl-1",
  model_id: "mdl-1",
  source_id: "src-1",
  table_type: "fact",
  physical_name: "fct_sales",
  alias: "sales",
  display_name: "Sales",
  description: null,
  row_count_estimate: 1000,
  last_stats_at: null,
  created_at: "2026-04-01T00:00:00Z",
  updated_at: "2026-04-01T00:00:00Z",
};

const fakePhysical: TableAttribute = {
  kind: "physical",
  id: "col-1",
  table_id: "tbl-1",
  name: "amount",
  display_name: null,
  description: null,
  is_hidden: false,
  is_primary_key: false,
  data_type: "numeric",
  is_user_defined: false,
  validated: true,
  validation_error: null,
};

function renderDialog() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TableEditDialog
        open
        onClose={() => {}}
        projectId="proj-1"
        modelId="mdl-1"
        sourceId="src-1"
        table={fakeTable}
        connectionId="conn-1"
      />
    </QueryClientProvider>,
  );
}

describe("TableEditDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListAttrs.mockResolvedValue([fakePhysical]);
    mockListUda.mockResolvedValue([]);
    mockFunctionCatalog.mockResolvedValue([]);
    mockUpdateTable.mockResolvedValue(undefined);
    mockUpdateColumn.mockResolvedValue(undefined);
    mockCreateUda.mockResolvedValue(undefined);
    mockListMeasures.mockResolvedValue([]);
    mockListDimensions.mockResolvedValue([]);
  });

  it("renders all four tabs and opens on Table Details by default", () => {
    renderDialog();
    expect(screen.getByRole("tab", { name: /table details/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /classification/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /business description/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /attributes/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /table details/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByLabelText(/^alias$/i)).toBeInTheDocument();
  });

  it("preserves the Table Details tab draft when switching tabs and back", async () => {
    const user = userEvent.setup();
    renderDialog();
    const aliasInput = screen.getByLabelText(/^alias$/i) as HTMLInputElement;
    await user.clear(aliasInput);
    await user.type(aliasInput, "sales_v2");
    expect(aliasInput.value).toBe("sales_v2");

    await user.click(screen.getByRole("tab", { name: /business description/i }));
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /business description/i })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );

    await user.click(screen.getByRole("tab", { name: /table details/i }));
    const aliasAfter = screen.getByLabelText(/^alias$/i) as HTMLInputElement;
    expect(aliasAfter.value).toBe("sales_v2");
  });

  it("Table Details Save calls modelTablesApi.update only", async () => {
    const user = userEvent.setup();
    renderDialog();
    const aliasInput = screen.getByLabelText(/^alias$/i);
    await user.clear(aliasInput);
    await user.type(aliasInput, "sales_v2");
    const saveBtn = screen.getByRole("button", { name: /^save$/i });
    await user.click(saveBtn);
    await waitFor(() => expect(mockUpdateTable).toHaveBeenCalledTimes(1));
    expect(mockUpdateTable).toHaveBeenCalledWith(
      "proj-1",
      "mdl-1",
      "src-1",
      "tbl-1",
      expect.objectContaining({ alias: "sales_v2" }),
    );
    expect(mockUpdateColumn).not.toHaveBeenCalled();
    expect(mockCreateUda).not.toHaveBeenCalled();
  });

  it("Business Description tab Save calls tableAttributesApi.updateColumn only", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("tab", { name: /business description/i }));

    // Both tabs are kept mounted, so disambiguate the per-column display name
    // field by its placeholder (the physical column name).
    const displayInput = await screen.findByPlaceholderText("amount");
    await user.type(displayInput, "Amount");

    // The bottom-of-tab Save button triggers updateColumn for every dirty draft.
    const saveAllBtn = await screen.findByRole("button", { name: /^save$/i });
    await user.click(saveAllBtn);

    await waitFor(() => expect(mockUpdateColumn).toHaveBeenCalledTimes(1));
    expect(mockUpdateColumn).toHaveBeenCalledWith(
      "proj-1",
      "mdl-1",
      "tbl-1",
      "col-1",
      expect.objectContaining({ display_name: "Amount" }),
    );
    expect(mockUpdateTable).not.toHaveBeenCalled();
    expect(mockCreateUda).not.toHaveBeenCalled();
  });

  it("locks the expression of a generated UDA on edit (F-016-06)", async () => {
    const user = userEvent.setup();
    const generatedAttr: TableAttribute = {
      kind: "user_defined",
      id: "uda-gen-1",
      table_id: "tbl-1",
      name: "payment_date_year",
      display_name: null,
      description: null,
      is_hidden: false,
      is_primary_key: false,
      data_type: "integer",
      is_user_defined: true,
      is_generated: true,
      expression: "EXTRACT(YEAR FROM (payment_date))",
      validated: true,
      validation_error: null,
    };
    mockListAttrs.mockResolvedValue([fakePhysical, generatedAttr]);
    mockListUda.mockResolvedValue([
      {
        id: "uda-gen-1",
        table_id: "tbl-1",
        model_id: "mdl-1",
        name: "payment_date_year",
        expression: "EXTRACT(YEAR FROM (payment_date))",
        output_data_type: "integer",
        description: null,
        validated: true,
        validation_error: null,
        is_generated: true,
        referenced_columns: ["payment_date"],
        created_at: "2026-04-01T00:00:00Z",
        updated_at: "2026-04-01T00:00:00Z",
      },
    ]);

    renderDialog();
    await user.click(screen.getByRole("tab", { name: /attributes/i }));

    const editBtn = await screen.findByRole("button", { name: /edit formula/i });
    await user.click(editBtn);

    // The expression textarea is disabled for generated UDAs; name stays editable.
    const exprField = await screen.findByDisplayValue("EXTRACT(YEAR FROM (payment_date))");
    expect(exprField).toBeDisabled();
    const nameField = screen.getByDisplayValue("payment_date_year");
    expect(nameField).not.toBeDisabled();
  });

  it("persists declared primary keys from the Business Description tab", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("tab", { name: /business description/i }));
    await user.click(await screen.findByRole("checkbox", { name: /declare amount as primary key/i }));
    await user.click(await screen.findByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(mockUpdateColumn).toHaveBeenCalledTimes(1));
    expect(mockUpdateColumn).toHaveBeenCalledWith(
      "proj-1",
      "mdl-1",
      "tbl-1",
      "col-1",
      expect.objectContaining({ is_primary_key: true }),
    );
  });
});
