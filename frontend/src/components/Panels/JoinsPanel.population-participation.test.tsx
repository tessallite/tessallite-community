import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";
import JoinsPanel from "./JoinsPanel";

/**
 * Bug-8615 governance phase G2 — the frontend Joins-panel half: a
 * "Population participation" selector, a diff preview before save, and the
 * model-level OK/WARNING/BLOCKED rollup banner + per-join badge fed by
 * GET .../join-population-health. This covers the behavior a deep review
 * flagged as untested: the two pre-existing JoinsPanel test files only added
 * mocks to keep old assertions passing, without exercising the new field.
 */
const api = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  delete: vi.fn(),
  listAttributes: vi.fn(),
  joinPopulationHealth: vi.fn(),
}));

vi.mock("../../api/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/hooks")>();
  return {
    ...actual,
    useSources: () => ({ data: [{ id: "source-1" }], isLoading: false }),
    useAllModelTables: () => ({
      data: [
        { id: "table-left", alias: "Orders", table_type: "fact" },
        { id: "table-right", alias: "Customers", table_type: "dim_detail" },
      ],
      isLoading: false,
    }),
  };
});

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return {
    ...actual,
    joinsApi: {
      ...actual.joinsApi,
      list: api.list,
      create: api.create,
      update: api.update,
      delete: api.delete,
    },
    tableAttributesApi: {
      ...actual.tableAttributesApi,
      list: api.listAttributes,
    },
    joinPopulationHealthApi: {
      ...actual.joinPopulationHealthApi,
      get: api.joinPopulationHealth,
    },
  };
});

vi.mock("../Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

const baseJoin = {
  id: "join-1",
  left_table_id: "table-left",
  right_table_id: "table-right",
  join_type: "left",
  left_column_id: "left-column",
  right_column_id: "right-column",
  left_column_name: "customer_id",
  right_column_name: "id",
  cardinality: "many_to_one",
  // Deliberately a NON-default value. If openEditDialog ever regressed to
  // hard-coding the "original" declaration to DEFAULT_POPULATION_PARTICIPATION
  // instead of reading it off the join, this value would make the diff-preview
  // assertion below fail (a value identical to the default could not tell the
  // two cases apart).
  population_participation: "enrichment_only",
};

function renderPanel() {
  useBuilderStore.getState().reset();
  useBuilderStore.getState().setReadOnly(false);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/projects/project-1/models/model-1"]}>
      <QueryClientProvider client={queryClient}>
        <I18nContext.Provider value={en}>
          <Routes>
            <Route path="/projects/:projectId/models/:modelId" element={<JoinsPanel />} />
          </Routes>
        </I18nContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Address a dialog select by its accessible label, not position (see
 * JoinsPanel.warnings-lifecycle.test.tsx for why: positional indexing breaks
 * the moment a new field is added between two existing ones). */
async function selectOption(label: RegExp, option: RegExp) {
  await waitFor(() => {
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("combobox", { name: label }),
    ).not.toHaveAttribute("aria-disabled", "true");
  });
  const dialog = screen.getByRole("dialog");
  fireEvent.mouseDown(within(dialog).getByRole("combobox", { name: label }));
  fireEvent.click(await screen.findByRole("option", { name: option }));
}

describe("JoinsPanel population participation (Bug-8615 G2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.list.mockResolvedValue([baseJoin]);
    api.listAttributes.mockImplementation(async (_projectId, _modelId, tableId) => (
      tableId === "table-left"
        ? [{ id: "left-column", name: "customer_id", data_type: "INTEGER" }]
        : [{ id: "right-column", name: "id", data_type: "VARCHAR" }]
    ));
    api.update.mockImplementation(async (_projectId, _modelId, _joinId, body) => ({
      ...baseJoin,
      ...body,
    }));
    api.joinPopulationHealth.mockResolvedValue({
      model_id: "model-1",
      status: "WARNING",
      evaluated: true,
      join_count: 1,
      evaluated_count: 1,
      warning_count: 1,
      blocked_count: 0,
      warn_only: true,
      items: [
        {
          join_id: "join-1",
          left_table_name: "Orders",
          right_table_name: "Customers",
          left_column_name: "customer_id",
          right_column_name: "id",
          join_type: "left",
          population_participation: "enrichment_only",
          checked_population_participation: "enrichment_only",
          declaration_changed_since_check: false,
          inputs_changed_since_check: false,
          classification: "filtering",
          status: "WARNING",
          measured: true,
          row_loss_ratio: 0.02,
          row_mult_ratio: 0,
          row_effect_ratio: 0.02,
          reason: "Row-filtering join with no declared intent.",
          checked_at: "2026-08-01T00:00:00Z",
          stale: false,
        },
      ],
    });
  });

  it("surfaces the model rollup banner and a per-join status badge from the health endpoint", async () => {
    renderPanel();
    await waitFor(() => expect(api.joinPopulationHealth).toHaveBeenCalledTimes(1));

    expect(await screen.findByText("Population governance")).toBeInTheDocument();
    expect(screen.getByText(/1 join\(s\) may be changing row counts/)).toBeInTheDocument();
    expect(screen.getByText("Row-count warning")).toBeInTheDocument();
  });

  it("shows a diff preview and saves the new declaration when the modeller changes it", async () => {
    renderPanel();

    fireEvent.click(await screen.findByTestId("join-edit-join-1"));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());

    // Nothing changed yet — no diff preview.
    expect(screen.queryByText(/Changing from/)).toBeNull();

    await selectOption(/Population role/i, /Defines the population/);

    // Diff preview reflects the ORIGINAL declaration read off the join
    // (baseJoin.population_participation = "enrichment_only", deliberately
    // NOT the default), not a re-derived/hard-coded default value.
    expect(
      await screen.findByText('Changing from "Adds detail only" to "Defines the population".'),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1));
    expect(api.update).toHaveBeenCalledWith(
      "project-1",
      "model-1",
      "join-1",
      expect.objectContaining({ population_participation: "population_defining" }),
    );
  });

  it("normalises a legacy join type before displaying or editing it", async () => {
    api.list.mockResolvedValue([{ ...baseJoin, join_type: "many_to_one" }]);
    renderPanel();

    expect(await screen.findByText("LEFT")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("join-edit-join-1"));

    const left = await screen.findByRole("radio", { name: /Left outer/i });
    expect(left).toBeChecked();
  });

  it("keeps cardinality and population participation in delete history", async () => {
    const actions: unknown[] = [];
    const listener = (event: Event) => {
      actions.push((event as CustomEvent).detail.action);
    };
    window.addEventListener("canvas-history-action", listener);
    api.delete.mockResolvedValue(undefined);

    renderPanel();
    fireEvent.click(await screen.findByTestId("join-delete-join-1"));
    await waitFor(() => expect(api.delete).toHaveBeenCalledTimes(1));

    expect(actions).toContainEqual(expect.objectContaining({
      type: "deleteLink",
      createData: expect.objectContaining({
        join_type: "left",
        cardinality: "many_to_one",
        population_participation: "enrichment_only",
      }),
    }));
    window.removeEventListener("canvas-history-action", listener);
  });
});
