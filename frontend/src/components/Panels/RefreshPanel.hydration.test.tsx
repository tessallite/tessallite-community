/**
 * Bug-8785 (sibling surface): the per-aggregate refresh dialog must hydrate the
 * persisted policy before it can write one back.
 *
 * policyPayload.ts is "the canonical producer for both aggregate refresh-policy
 * authoring surfaces". The other surface (AggregatesPanel RefreshTab) was fixed
 * for the reported defect; this one opened with component defaults, never
 * called getPolicy, and then wrote the full payload — including
 * incremental_append_only — back from those unhydrated constants. Saving any
 * change on an append-only aggregate silently cleared the append-only authority
 * this wave introduced.
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
  schedulerApiClient: { triggerRefresh: vi.fn() },
}));

vi.mock("../../api/hooks", () => ({
  useAggregates: (...a: unknown[]) => useAggregatesMock(...a),
}));

vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "p", modelId: "m" }),
}));

import RefreshPanel from "./RefreshPanel";

const AGG_ID = "agg-1";
const APPEND_ONLY_POLICY = {
  id: "p1",
  aggregate_definition_id: AGG_ID,
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

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <RefreshPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAggregatesMock.mockReturnValue({
    data: [
      {
        id: AGG_ID,
        status: "active",
        physical_table_name: "agg_inc",
        grain: ["day"],
      },
    ],
    isLoading: false,
  });
  getPolicyMock.mockResolvedValue(APPEND_ONLY_POLICY);
  setPolicyMock.mockResolvedValue({});
});

describe("Bug-8785 sibling surface: RefreshPanel policy hydration", () => {
  it("reads the persisted policy when the schedule dialog is opened", async () => {
    renderPanel();
    const user = userEvent.setup();

    const scheduleBtn = await screen.findByRole("button", { name: /schedule/i });
    await user.click(scheduleBtn);

    // The defect: this call never happened, so the dialog showed — and saved —
    // full/blank defaults over a persisted incremental append-only policy.
    await waitFor(() =>
      expect(getPolicyMock).toHaveBeenCalledWith("p", "m", AGG_ID),
    );
  });

  it("shows the persisted incremental column instead of a blank default", async () => {
    renderPanel();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /schedule/i }));
    await waitFor(() => expect(getPolicyMock).toHaveBeenCalled());

    // business_date comes only from the server; a defaulted dialog renders "".
    await waitFor(() => {
      const hydrated = screen
        .getAllByRole("textbox")
        .some((el) => (el as HTMLInputElement).value === "business_date");
      expect(hydrated).toBe(true);
    });
  });
});
