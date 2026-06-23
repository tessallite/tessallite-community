import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const analyzeMock = vi.fn();
const updateTableMock = vi.fn();
const updateColumnMock = vi.fn();
const listMeasuresMock = vi.fn();
const listDimensionsMock = vi.fn();
const createMeasureMock = vi.fn();
const createDimensionMock = vi.fn();

vi.mock("../api/client", () => ({
  modelTablesApi: {
    analyze: (...args: unknown[]) => analyzeMock(...args),
    update: (...args: unknown[]) => updateTableMock(...args),
  },
  tableAttributesApi: {
    updateColumn: (...args: unknown[]) => updateColumnMock(...args),
  },
  measuresApi: {
    list: (...args: unknown[]) => listMeasuresMock(...args),
    create: (...args: unknown[]) => createMeasureMock(...args),
  },
  dimensionsApi: {
    list: (...args: unknown[]) => listDimensionsMock(...args),
    create: (...args: unknown[]) => createDimensionMock(...args),
  },
}));

import TableAnalysisDialog from "./TableAnalysisDialog";

const PROPS = {
  open: true,
  onClose: vi.fn(),
  projectId: "proj-1",
  modelId: "model-1",
  sourceId: "src-1",
  tableId: "tbl-1",
  tableName: "orders",
};

const ANALYSIS_RESULT = {
  table_id: "tbl-1",
  suggested_table_type: "fact",
  confidence: "high",
  reasoning: "Looks like a fact table",
  column_suggestions: [
    { column_id: "c1", column_name: "revenue", suggested_role: "measure", reason: "numeric" },
    { column_id: "c2", column_name: "quantity", suggested_role: "measure", reason: "numeric" },
    { column_id: "c3", column_name: "region", suggested_role: "dimension", reason: "low cardinality" },
    { column_id: "c4", column_name: "order_date", suggested_role: "date_key", reason: "date type" },
    { column_id: "c5", column_name: "internal_id", suggested_role: "ignore", reason: "system column" },
  ],
  date_columns: ["order_date"],
  potential_calendar_column: "order_date",
};

function renderDialog(props = PROPS) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TableAnalysisDialog {...props} />
    </QueryClientProvider>,
  );
}

describe("TableAnalysisDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listMeasuresMock.mockResolvedValue([]);
    listDimensionsMock.mockResolvedValue([]);
    createMeasureMock.mockResolvedValue({});
    createDimensionMock.mockResolvedValue({});
    updateTableMock.mockResolvedValue({});
    updateColumnMock.mockResolvedValue({});
  });

  it("creates measures for columns with role 'measure'", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getByText("revenue")).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(createMeasureMock).toHaveBeenCalledTimes(2);
    });

    expect(createMeasureMock).toHaveBeenCalledWith("proj-1", "model-1", {
      name: "revenue",
      display_name: "Revenue",
      source_table_id: "tbl-1",
      source_column_name: "revenue",
      default_agg: "sum",
    });
    expect(createMeasureMock).toHaveBeenCalledWith("proj-1", "model-1", {
      name: "quantity",
      display_name: "Quantity",
      source_table_id: "tbl-1",
      source_column_name: "quantity",
      default_agg: "sum",
    });
  });

  it("creates dimensions for columns with role 'dimension'", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getByText("region")).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(createDimensionMock).toHaveBeenCalledWith("proj-1", "model-1", {
        name: "region",
        display_name: "Region",
        source_table_id: "tbl-1",
        source_column_name: "region",
      });
    });
  });

  it("creates time dimension for columns with role 'date_key'", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getAllByText("order_date").length).toBeGreaterThan(0);
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(createDimensionMock).toHaveBeenCalledWith("proj-1", "model-1", {
        name: "order_date",
        display_name: "Order Date",
        source_table_id: "tbl-1",
        source_column_name: "order_date",
        is_time_dim: true,
      });
    });
  });

  it("hides columns with role 'ignore'", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getByText("internal_id")).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(updateColumnMock).toHaveBeenCalledWith(
        "proj-1", "model-1", "tbl-1", "c5",
        { is_hidden: true },
      );
    });
  });

  it("skips measure creation when a measure already exists for that column", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    listMeasuresMock.mockResolvedValue([
      { id: "m-existing", name: "revenue", source_table_id: "tbl-1", source_column_name: "revenue" },
    ]);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getByText("revenue")).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(createMeasureMock).toHaveBeenCalledTimes(1);
    });
    expect(createMeasureMock).toHaveBeenCalledWith("proj-1", "model-1",
      expect.objectContaining({ name: "quantity" }),
    );
  });

  it("skips dimension creation when a dimension already exists for that column", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    listDimensionsMock.mockResolvedValue([
      { id: "d-existing", name: "region", source_table_id: "tbl-1", source_column_name: "region" },
    ]);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getByText("region")).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(screen.getByText(/suggestions applied/i)).toBeTruthy();
    });
    const regionCalls = createDimensionMock.mock.calls.filter(
      (c: unknown[]) => (c[2] as { name: string }).name === "region",
    );
    expect(regionCalls).toHaveLength(0);
  });

  it("shows success message after apply", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getByText("revenue")).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(screen.getByText(/measures and dimensions created/i)).toBeTruthy();
    });
  });

  it("updates table type on apply", async () => {
    const user = userEvent.setup();
    analyzeMock.mockResolvedValue(ANALYSIS_RESULT);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /run analysis/i }));
    await waitFor(() => {
      expect(screen.getByText("revenue")).toBeTruthy();
    });

    await user.click(screen.getByRole("button", { name: /apply suggestions/i }));
    await waitFor(() => {
      expect(updateTableMock).toHaveBeenCalledWith(
        "proj-1", "model-1", "src-1", "tbl-1",
        { table_type: "fact" },
      );
    });
  });
});
