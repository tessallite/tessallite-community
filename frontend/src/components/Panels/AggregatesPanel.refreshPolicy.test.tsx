/**
 * Bug-8785: the aggregate refresh editor must never overwrite a persisted
 * policy the user did not edit.
 *
 * The editor seeded every row from a local daily/full/blank default and then
 * POSTed EVERY active aggregate on save. `aggregatesApi.getPolicy` was never
 * called, so an aggregate carrying a persisted incremental/append-only policy
 * displayed as "full" and was cleared the moment the user saved an unrelated
 * change on a different row.
 *
 * Guard: these tests. Tier: T1.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const getPolicyMock = vi.fn();
const setPolicyMock = vi.fn();
const useAggregatesMock = vi.fn();

vi.mock("../../api/client", () => ({
  aggregatesApi: {
    getPolicy: (...a: unknown[]) => getPolicyMock(...a),
    setPolicy: (...a: unknown[]) => setPolicyMock(...a),
  },
}));

vi.mock("../../api/hooks", () => ({
  useAggregates: (...a: unknown[]) => useAggregatesMock(...a),
}));

import { RefreshTab } from "./AggregatesPanel";

const INCREMENTAL_AGG = "agg-incremental";
const OTHER_AGG = "agg-other";

function persistedIncrementalPolicy() {
  return {
    id: "p1",
    aggregate_definition_id: INCREMENTAL_AGG,
    refresh_mode: "incremental",
    cron_expression: "0 2 * * *",
    incremental_column: "business_date",
    incremental_lookback: 7,
    incremental_append_only: true,
    full_rebuild_interval_days: 30,
    is_enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <RefreshTab projectId="p" modelId="m" canEdit />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAggregatesMock.mockReturnValue({
    data: [
      { id: INCREMENTAL_AGG, status: "active", physical_table_name: "agg_inc" },
      { id: OTHER_AGG, status: "active", physical_table_name: "agg_other" },
    ],
    isLoading: false,
  });
  getPolicyMock.mockImplementation(async (_p: string, _m: string, aggId: string) => {
    if (aggId === INCREMENTAL_AGG) return persistedIncrementalPolicy();
    throw new Error("404 no policy configured");
  });
  setPolicyMock.mockResolvedValue({});
});

describe("Bug-8785 refresh policy hydration", () => {
  it("reads the persisted policy for every active aggregate", async () => {
    renderTab();
    await waitFor(() => expect(getPolicyMock).toHaveBeenCalledWith("p", "m", INCREMENTAL_AGG));
    expect(getPolicyMock).toHaveBeenCalledWith("p", "m", OTHER_AGG);
  });

  it("saves nothing when the user edited nothing", async () => {
    renderTab();
    await waitFor(() => expect(getPolicyMock).toHaveBeenCalled());

    const save = await screen.findByTestId("refresh-save-all");
    // With no edits there is nothing to persist, so the control is inert.
    expect(save).toBeDisabled();
    expect(setPolicyMock).not.toHaveBeenCalled();
  });

  it("does NOT write the untouched aggregate when another row is edited", async () => {
    // This is the defect: editing agg_other used to POST a default
    // daily/full/blank policy for agg_inc as well, destroying append-only.
    renderTab();
    await waitFor(() => expect(getPolicyMock).toHaveBeenCalled());

    const otherRow = await screen.findByTestId(`refresh-schedule-${OTHER_AGG}`);
    const combos = otherRow.querySelectorAll("input, [role='combobox']");
    expect(combos.length).toBeGreaterThan(0);

    const user = userEvent.setup();
    const incrementalColumn = otherRow.querySelector("input[type='text']");
    if (incrementalColumn) await user.type(incrementalColumn, "x");

    const save = await screen.findByTestId("refresh-save-all");
    if (!(save as HTMLButtonElement).disabled) await user.click(save);

    await waitFor(() => {
      const written = setPolicyMock.mock.calls.map((c) => c[2]);
      expect(written).not.toContain(INCREMENTAL_AGG);
    });
  });

  it("round-trips the persisted policy unchanged when that row IS saved", async () => {
    renderTab();
    await waitFor(() => expect(getPolicyMock).toHaveBeenCalled());

    const row = await screen.findByTestId(`refresh-schedule-${INCREMENTAL_AGG}`);
    const user = userEvent.setup();
    const textInput = row.querySelector("input[type='text']");
    if (textInput) {
      await user.clear(textInput);
      await user.type(textInput, "business_date");
    }

    const save = await screen.findByTestId("refresh-save-all");
    if (!(save as HTMLButtonElement).disabled) await user.click(save);

    await waitFor(() => expect(setPolicyMock).toHaveBeenCalled());
    const call = setPolicyMock.mock.calls.find((c) => c[2] === INCREMENTAL_AGG);
    expect(call, "the edited aggregate must be the one written").toBeTruthy();
    // The persisted incremental authority survives a save of that row.
    expect(call![3].refresh_mode).toBe("incremental");
    expect(call![3].incremental_append_only).toBe(true);
  });
});
