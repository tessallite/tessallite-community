import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const alertsListMock = vi.fn();

vi.mock("../../api/client", () => ({
  alertsApi: {
    list: (...args: unknown[]) => alertsListMock(...args),
  },
}));

import type { Join, ModelAlert, ModelTable } from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import ValidationTray from "./ValidationTray";
import StatusBar from "./StatusBar";
import { useModelValidation } from "./useModelValidation";

/** Mock generated from the backend ModelAlertResponse schema
 *  (shared/schemas/domains/dimensions_measures.py). */
const BROKEN_DIMENSION_ALERT: ModelAlert = {
  id: "11111111-1111-1111-1111-111111111111",
  model_id: "m1",
  severity: "warning",
  category: "invalid_dimension",
  title: "Dimension is structurally invalid",
  detail: 'Dimension "Region" references missing column region_code',
  related_object_type: "dimension",
  related_object_id: "22222222-2222-2222-2222-222222222222",
  first_seen_at: "2026-06-12T08:00:00Z",
  last_seen_at: "2026-06-12T09:00:00Z",
  occurrence_count: 3,
  resolved_at: null,
  dismissed_at: null,
  dismissed_by: null,
};

function table(partial: Partial<ModelTable> & { id: string }): ModelTable {
  return {
    model_id: "m1",
    source_id: "src-1",
    table_type: "dimension",
    physical_name: partial.id,
    alias: partial.id,
    display_name: partial.id,
    row_count_estimate: null,
    last_stats_at: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    ...partial,
  };
}

const HEALTHY_TABLES: ModelTable[] = [
  table({ id: "fact-1", table_type: "fact" }),
  table({ id: "dim-1" }),
];
const HEALTHY_JOINS: Join[] = [
  {
    id: "join-1",
    left_table_id: "fact-1",
    right_table_id: "dim-1",
    join_type: "inner",
    left_column_id: "c1",
    right_column_id: "c2",
    left_column_name: "dim_id",
    right_column_name: "id",
  },
];

function Harness({
  tables = HEALTHY_TABLES,
  joins = HEALTHY_JOINS,
  hasTarget = true,
}: {
  tables?: ModelTable[];
  joins?: Join[];
  hasTarget?: boolean;
}) {
  useModelValidation("p1", "m1", { tables, joins, hasTarget, ready: true });
  return (
    <>
      <ValidationTray />
      <StatusBar
        tableCount={tables.length}
        joinCount={joins.length}
        dimCount={0}
        measCount={0}
        aggCount={0}
      />
    </>
  );
}

function renderHarness(props: Parameters<typeof Harness>[0] = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
}

describe("Validation tray wiring (F-026-01)", () => {
  beforeEach(() => {
    alertsListMock.mockReset();
    useBuilderStore.getState().reset();
    // Tray content is asserted directly, so keep it expanded.
    useBuilderStore.setState({ validationExpanded: true });
  });

  it("a model with a broken dimension reported by the validation engine shows that problem in the tray", async () => {
    alertsListMock.mockResolvedValue([BROKEN_DIMENSION_ALERT]);
    renderHarness();

    expect(
      await screen.findByText(
        'Invalid dimension: Dimension "Region" references missing column region_code',
      ),
    ).toBeTruthy();
    // Status chip reflects the warning instead of claiming a clean model.
    await waitFor(() => {
      expect(screen.getByTestId("validation-summary").textContent).toContain(
        "1 warning",
      );
    });
    expect(screen.queryByText("No validation issues")).toBeNull();
  });

  it("clicking the broken-dimension issue navigates to the dimension in its panel", async () => {
    alertsListMock.mockResolvedValue([BROKEN_DIMENSION_ALERT]);
    renderHarness();

    const issue = await screen.findByText(
      'Invalid dimension: Dimension "Region" references missing column region_code',
    );
    await userEvent.click(issue);

    const state = useBuilderStore.getState();
    expect(state.activePanel).toBe("dimensions");
    expect(state.selectedObjectId).toBe(
      "22222222-2222-2222-2222-222222222222",
    );
    expect(state.selectedObjectType).toBe("dimension");
  });

  it("a structurally healthy model with no alerts reports no validation issues", async () => {
    alertsListMock.mockResolvedValue([]);
    renderHarness();

    await waitFor(() => expect(alertsListMock).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("validation-summary").textContent).toBe(
        "No validation issues",
      );
    });
  });

  it("a model whose table is joined to nothing shows the isolated-table warning, and clicking it centres the canvas and focuses the Sources panel", async () => {
    alertsListMock.mockResolvedValue([]);
    const orphan = table({
      id: "dim-orphan",
      display_name: "Orphan Dim",
      source_id: "src-9",
    });
    renderHarness({ tables: [...HEALTHY_TABLES, orphan] });

    const issue = await screen.findByText(
      'Table "Orphan Dim" is not joined to any other table',
    );

    const centerEvents: string[] = [];
    const listener = (e: Event) =>
      centerEvents.push((e as CustomEvent<string>).detail);
    window.addEventListener("canvas-center-node", listener);
    try {
      await userEvent.click(issue);
    } finally {
      window.removeEventListener("canvas-center-node", listener);
    }
    expect(centerEvents).toEqual(["dim-orphan"]);

    // The issue carries the owning source, so navigation also opens the
    // Sources panel and highlights the table's row (LOW-4: sourceId consumed).
    const state = useBuilderStore.getState();
    expect(state.activePanel).toBe("sources");
    expect(state.focusedTableId).toBe("dim-orphan");
    expect(state.focusedSourceId).toBe("src-9");
  });

  it("a model with no fact table shows the no-fact-table warning", async () => {
    alertsListMock.mockResolvedValue([]);
    renderHarness({
      tables: [table({ id: "dim-a" }), table({ id: "dim-b" })],
      joins: [
        {
          id: "join-ab",
          left_table_id: "dim-a",
          right_table_id: "dim-b",
          join_type: "inner",
          left_column_id: "c1",
          right_column_id: "c2",
          left_column_name: "id",
          right_column_name: "a_id",
        },
      ],
    });

    expect(
      await screen.findByText(/No fact table — classify at least one table/),
    ).toBeTruthy();
  });

  it("a model without a query target shows an informational notice", async () => {
    alertsListMock.mockResolvedValue([]);
    renderHarness({ hasTarget: false });

    expect(
      await screen.findByText(/No query target set/),
    ).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByTestId("validation-summary").textContent).toContain(
        "1 notice",
      );
    });
  });
});
