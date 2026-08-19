import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";
import JoinsPanel from "./JoinsPanel";

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

/**
 * Pick an option from one of the dialog's selects, addressed by its LABEL.
 *
 * This used to index into ``getAllByRole("combobox")`` positionally, which
 * silently retargets every later field the moment a new control is added to
 * the dialog — adding the join Cardinality select between the join-type radios
 * and the right-table select made "index 2" mean the cardinality rather than
 * the right table. Addressing by label makes the helper independent of the
 * dialog's field order.
 */
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

describe("JoinsPanel authoritative warning lifecycle (Bug-8094)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    let rows: Array<typeof baseJoin & { warnings?: string[] }> = [];
    let updateCount = 0;
    api.list.mockImplementation(async () => rows);
    api.joinPopulationHealth.mockResolvedValue({
      model_id: "model-1",
      status: "OK",
      evaluated: true,
      join_count: 0,
      evaluated_count: 0,
      warning_count: 0,
      blocked_count: 0,
      warn_only: true,
      items: [],
    });
    api.listAttributes.mockImplementation(async (_projectId, _modelId, tableId) => (
      tableId === "table-left"
        ? [{ id: "left-column", name: "customer_id", data_type: "INTEGER" }]
        : [{ id: "right-column", name: "id", data_type: "VARCHAR" }]
    ));
    api.create.mockImplementation(async () => {
      rows = [{ ...baseJoin, warnings: ["Created join warning"] }];
      return rows[0];
    });
    api.update.mockImplementation(async () => {
      updateCount += 1;
      rows = [{
        ...baseJoin,
        warnings: updateCount === 1
          ? ["Updated warning one", "Updated warning two"]
          : [],
      }];
      return rows[0];
    });
  });

  it("refetches none -> one -> many -> none after create and update responses", async () => {
    renderPanel();
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("status", { name: "Join validation warnings" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await selectOption(/Left Table/i, /Orders/);
    await selectOption(/Left Column/i, /customer_id/);
    await selectOption(/Right Table/i, /Customers/);
    await selectOption(/Right Column/i, /^id/);
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    let warningStatus = await screen.findByRole("status", { name: "Join validation warnings" });
    expect(within(warningStatus).getAllByRole("listitem")).toHaveLength(1);
    expect(within(warningStatus).getByText("Created join warning")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("join-edit-join-1"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      warningStatus = screen.getByRole("status", { name: "Join validation warnings" });
      expect(within(warningStatus).getAllByRole("listitem")).toHaveLength(2);
    });
    expect(within(warningStatus).getByText("Updated warning one")).toBeInTheDocument();
    expect(within(warningStatus).getByText("Updated warning two")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("join-edit-join-1"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(screen.queryByRole("status", { name: "Join validation warnings" })).toBeNull();
    });
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.update).toHaveBeenCalledTimes(2);
    expect(api.list).toHaveBeenCalledTimes(4);
  });
});
