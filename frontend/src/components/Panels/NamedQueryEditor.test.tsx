import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ComponentProps } from "react";
import type { NamedQuery } from "../../api/types";
import NamedQueryEditor from "./NamedQueryEditor";

const listNqMock = vi.fn();
const createNqMock = vi.fn();
const updateNqMock = vi.fn();
const validateNqMock = vi.fn();
const refreshNqMock = vi.fn();
const listRunsMock = vi.fn();
const putPolicyMock = vi.fn();
const listSettingsMock = vi.fn();
const analyticsNqMock = vi.fn();

vi.mock("../../api/client", () => ({
  namedQueriesApi: {
    list: (...args: unknown[]) => listNqMock(...args),
    get: vi.fn(),
    analytics: (...args: unknown[]) => analyticsNqMock(...args),
    create: (...args: unknown[]) => createNqMock(...args),
    update: (...args: unknown[]) => updateNqMock(...args),
    delete: vi.fn(),
    validate: (...args: unknown[]) => validateNqMock(...args),
    refresh: (...args: unknown[]) => refreshNqMock(...args),
    listRuns: (...args: unknown[]) => listRunsMock(...args),
    getPolicy: vi.fn(),
    putPolicy: (...args: unknown[]) => putPolicyMock(...args),
  },
  projectSettingsApi: {
    list: (...args: unknown[]) => listSettingsMock(...args),
  },
}));

vi.mock("../../auth/currentUser", () => ({
  isTenantAdmin: () => true,
}));

const SAMPLE_NQ: NamedQuery = {
  id: "nq1",
  model_id: "m-1",
  name: "top_cities",
  display_name: "Top Cities",
  description: "Top cities by transactions",
  display_folder: null,
  definition_sql: "SELECT city_name, SUM(transaction_value) AS total FROM modely GROUP BY city_name",
  output_columns: [
    { name: "city_name", type: "string" },
    { name: "total", type: "number" },
  ],
  shape: "aggregated",
  row_cap: null,
  column_cap: null,
  certification_status: "draft",
  created_by: null,
  artifact: null,
  refresh_policy: null,
  created_at: "2026-01-01",
  updated_at: "2026-01-01",
};

const SETTINGS = [
  { key: "named_query.max_rows", effective_value: 100000 },
  { key: "named_query.max_columns", effective_value: 200 },
  { key: "other.thing", effective_value: "x" },
];

function renderEditor(props: Partial<ComponentProps<typeof NamedQueryEditor>> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const merged: ComponentProps<typeof NamedQueryEditor> = {
    open: true,
    mode: "create",
    initial: null,
    projectId: "p-1",
    modelId: "m-1",
    canEdit: true,
    needsSaveOrDeploy: false,
    onClose: vi.fn(),
    ...props,
  };
  const result = render(
    <QueryClientProvider client={qc}>
      <NamedQueryEditor {...merged} />
    </QueryClientProvider>,
  );
  return { ...result, props: merged };
}

describe("NamedQueryEditor", () => {
  beforeEach(() => {
    listNqMock.mockReset();
    listNqMock.mockResolvedValue([]);
    createNqMock.mockReset();
    updateNqMock.mockReset();
    validateNqMock.mockReset();
    refreshNqMock.mockReset();
    listRunsMock.mockReset();
    listRunsMock.mockResolvedValue([]);
    analyticsNqMock.mockReset();
    analyticsNqMock.mockResolvedValue({
      named_query_id: "nq1",
      window_days: 30,
      total_queries: 0,
      materialized_queries: 0,
      fallback_queries: 0,
      fallback_failures: 0,
      fallback_rate: 0,
      avg_fallback_execution_ms: null,
      avg_fallback_bytes_processed: null,
      fallback_reasons: [],
      recommendation: "none",
      recommendation_reason: null,
    });
    putPolicyMock.mockReset();
    putPolicyMock.mockResolvedValue({ cron_expression: "0 6 * * *", is_enabled: true });
    listSettingsMock.mockReset();
    listSettingsMock.mockResolvedValue(SETTINGS);
  });

  it("renders create mode with name and SQL fields", () => {
    renderEditor();
    expect(screen.getByText("Add Named Query")).toBeTruthy();
    expect(screen.getByLabelText("Name")).toBeTruthy();
    expect(screen.getByTestId("nq-definition-sql")).toBeTruthy();
  });

  it("validates and shows output columns, shape and the caps count", async () => {
    validateNqMock.mockResolvedValue({
      is_valid: true,
      errors: [],
      warnings: [],
      output_columns: [
        { name: "city_name", type: "string" },
        { name: "total", type: "number" },
      ],
      shape: "aggregated",
    });
    renderEditor();
    const user = userEvent.setup();

    await user.type(screen.getByTestId("nq-definition-sql"), "SELECT 1");
    await user.click(screen.getByTestId("nq-validate-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("nq-validation-ok")).toBeTruthy();
    });
    expect(screen.getByTestId("nq-validation-ok").textContent).toContain("aggregated");
    expect(screen.getByTestId("nq-validation-ok").textContent).toContain("2 columns");

    const table = screen.getByTestId("nq-output-columns");
    expect(within(table).getByText("city_name")).toBeTruthy();
    expect(within(table).getByText("number")).toBeTruthy();

    // Caps: 2 of 200 columns · Row cap: 100000 rows (from project settings).
    const caps = screen.getByTestId("nq-caps-count");
    expect(caps.textContent).toContain("2 of 200 columns");
    expect(caps.textContent).toContain("Row cap: 100000 rows");
    expect(validateNqMock).toHaveBeenCalledWith("p-1", "m-1", {
      definition_sql: "SELECT 1",
    });
  });

  it("shows validation errors when the definition is invalid", async () => {
    validateNqMock.mockResolvedValue({
      is_valid: false,
      errors: ["Unsupported shape"],
      warnings: [],
      output_columns: [],
      shape: null,
    });
    renderEditor();
    const user = userEvent.setup();

    await user.type(screen.getByTestId("nq-definition-sql"), "DELETE FROM modely");
    await user.click(screen.getByTestId("nq-validate-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("nq-validation-invalid")).toBeTruthy();
    });
    expect(screen.getByTestId("nq-validation-invalid").textContent).toContain(
      "Unsupported shape",
    );
    expect(screen.queryByTestId("nq-validation-ok")).toBeNull();
  });

  it("creates a Named Query from the form", async () => {
    createNqMock.mockResolvedValue({ ...SAMPLE_NQ, id: "nq-new" });
    const { props } = renderEditor();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Name"), "top_cities");
    await user.type(screen.getByTestId("nq-definition-sql"), "SELECT city_name FROM modely");
    await user.click(screen.getByTestId("nq-save-btn"));

    await waitFor(() => {
      expect(createNqMock).toHaveBeenCalled();
    });
    const payload = createNqMock.mock.calls[0][2] as Record<string, unknown>;
    expect(payload.name).toBe("top_cities");
    expect(payload.definition_sql).toBe("SELECT city_name FROM modely");
    expect(props.onClose).toHaveBeenCalled();
  });

  it("F-026-01: create sends the refresh schedule when enabled with a cron", async () => {
    createNqMock.mockResolvedValue({ ...SAMPLE_NQ, id: "nq-new" });
    renderEditor();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Name"), "top_cities");
    await user.type(screen.getByTestId("nq-definition-sql"), "SELECT city_name FROM modely");
    await user.click(screen.getByTestId("nq-schedule-enabled"));
    await user.type(screen.getByTestId("nq-schedule-cron"), "0 6 * * *");
    await user.click(screen.getByTestId("nq-save-btn"));

    await waitFor(() => expect(createNqMock).toHaveBeenCalled());
    const payload = createNqMock.mock.calls[0][2] as Record<string, unknown>;
    expect(payload.refresh_policy).toBe("schedule");
    expect(payload.refresh_cron).toBe("0 6 * * *");
    expect(payload.refresh_policy_enabled).toBe(true);
  });

  it("F-026-01: create omits schedule fields when the toggle is off", async () => {
    createNqMock.mockResolvedValue({ ...SAMPLE_NQ, id: "nq-new" });
    renderEditor();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Name"), "top_cities");
    await user.type(screen.getByTestId("nq-definition-sql"), "SELECT city_name FROM modely");
    await user.click(screen.getByTestId("nq-save-btn"));

    await waitFor(() => expect(createNqMock).toHaveBeenCalled());
    const payload = createNqMock.mock.calls[0][2] as Record<string, unknown>;
    expect(payload.refresh_policy).toBeUndefined();
    expect(payload.refresh_cron).toBeUndefined();
  });

  it("F-026-01: enabling the schedule with a blank cron disables Save", async () => {
    renderEditor();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Name"), "top_cities");
    await user.type(screen.getByTestId("nq-definition-sql"), "SELECT city_name FROM modely");
    await user.click(screen.getByTestId("nq-schedule-enabled"));

    expect((screen.getByTestId("nq-save-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("F-026-01: editing the cron persists it through the policy resource", async () => {
    updateNqMock.mockResolvedValue({ ...SAMPLE_NQ });
    const scheduled: NamedQuery = {
      ...SAMPLE_NQ,
      refresh_policy: { cron_expression: "0 6 * * *", is_enabled: true },
    };
    renderEditor({ mode: "edit", initial: scheduled });
    const user = userEvent.setup();

    const cron = screen.getByTestId("nq-schedule-cron") as HTMLInputElement;
    await user.clear(cron);
    await user.type(cron, "0 9 * * *");
    await user.click(screen.getByTestId("nq-save-btn"));

    await waitFor(() => expect(putPolicyMock).toHaveBeenCalled());
    expect(putPolicyMock).toHaveBeenCalledWith("p-1", "m-1", "nq1", {
      cron_expression: "0 9 * * *",
      is_enabled: true,
    });
  });

  it("F-026-01: an edit that leaves the schedule untouched does not call putPolicy", async () => {
    updateNqMock.mockResolvedValue({ ...SAMPLE_NQ });
    const scheduled: NamedQuery = {
      ...SAMPLE_NQ,
      refresh_policy: { cron_expression: "0 6 * * *", is_enabled: true },
    };
    renderEditor({ mode: "edit", initial: scheduled });
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Display name"), " X");
    await user.click(screen.getByTestId("nq-save-btn"));

    await waitFor(() => expect(updateNqMock).toHaveBeenCalled());
    expect(putPolicyMock).not.toHaveBeenCalled();
  });

  it("edit mode shows health badge, refresh and last-refreshed", async () => {
    const withArtifact: NamedQuery = {
      ...SAMPLE_NQ,
      artifact: {
        id: "a1",
        target_id: "t1",
        physical_table_name: "nq_top_cities",
        target_schema: null,
        row_count: 3,
        status: "fresh",
        failure_reason: null,
        last_refresh_at: "2026-08-01T00:00:00Z",
        retired_at: null,
      },
    };
    listNqMock.mockResolvedValue([withArtifact]);
    renderEditor({ mode: "edit", initial: withArtifact });

    await waitFor(() => {
      expect(screen.getByTestId("nq-health-badge").textContent).toBe("Fresh");
    });
    expect(screen.getByTestId("nq-last-refreshed").textContent).toContain(
      "Last refreshed:",
    );
    expect(screen.getByTestId("nq-row-count").textContent).toContain("3 rows");
    expect(screen.getByTestId("nq-refresh-btn")).toBeTruthy();
    expect(screen.queryByTestId("nq-health-reason")).toBeNull();
  });

  it("edit mode surfaces a failed artifact with its reason", () => {
    const failed: NamedQuery = {
      ...SAMPLE_NQ,
      artifact: {
        id: "a1",
        target_id: "t1",
        physical_table_name: "nq_top_cities",
        target_schema: null,
        row_count: null,
        status: "failed",
        failure_reason: "ROW_CAP_EXCEEDED",
        last_refresh_at: null,
        retired_at: null,
      },
    };
    renderEditor({ mode: "edit", initial: failed });
    expect(screen.getByTestId("nq-health-badge").textContent).toBe("Failed");
    expect(screen.getByTestId("nq-health-reason").textContent).toContain(
      "ROW_CAP_EXCEEDED",
    );
  });

  it("never-refreshed artifact shows stale with the never-refreshed reason", () => {
    renderEditor({ mode: "edit", initial: SAMPLE_NQ });
    expect(screen.getByTestId("nq-health-badge").textContent).toBe("Stale");
    expect(screen.getByTestId("nq-health-reason").textContent).toContain(
      "Never materialised",
    );
  });

  it("shows attributed fallback analytics and the Named Query repair recommendation (Bug-9172)", async () => {
    analyticsNqMock.mockResolvedValue({
      named_query_id: "nq1",
      window_days: 30,
      total_queries: 4,
      materialized_queries: 1,
      fallback_queries: 3,
      fallback_failures: 0,
      fallback_rate: 75,
      avg_fallback_execution_ms: 120,
      avg_fallback_bytes_processed: 4096,
      fallback_reasons: [{ reason: "artifact_not_fresh", count: 3 }],
      recommendation: "repair_named_query_materialisation",
      recommendation_reason: "sustained_expensive_fallback",
    });
    renderEditor({ mode: "edit", initial: SAMPLE_NQ });

    await waitFor(() => {
      expect(screen.getByTestId("nq-analytics")).toBeTruthy();
    });
    expect(screen.getByTestId("nq-analytics").textContent).toContain(
      "3 of 4 queries fell back to the source (75.0%).",
    );
    expect(screen.getByTestId("nq-analytics").textContent).toContain("120.0 ms");
    expect(screen.getByTestId("nq-analytics").textContent).toContain("4,096 bytes");
    expect(screen.getByTestId("nq-analytics").textContent).toContain(
      "artifact_not_fresh (3)",
    );
    expect(screen.getByTestId("nq-analytics-recommendation").textContent).toContain(
      "Repair, refresh, or adjust this Named Query's materialisation",
    );
  });

  it("refresh queues a run and shows the queued alert", async () => {
    const fresh: NamedQuery = {
      ...SAMPLE_NQ,
      artifact: {
        id: "a1",
        target_id: "t1",
        physical_table_name: "nq_top_cities",
        target_schema: null,
        row_count: 3,
        status: "fresh",
        failure_reason: null,
        last_refresh_at: null,
        retired_at: null,
      },
    };
    listNqMock.mockResolvedValue([fresh]);
    refreshNqMock.mockResolvedValue({
      id: "run-1",
      named_query_id: "nq1",
      refresh_mode: "full",
      status: "queued",
      started_at: "2026-08-01T00:00:00Z",
      completed_at: null,
      rows_written: null,
      bytes_processed: null,
      error_message: null,
      triggered_by: "api",
    });
    renderEditor({ mode: "edit", initial: fresh });
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByTestId("nq-refresh-btn")).toBeTruthy();
    });
    await user.click(screen.getByTestId("nq-refresh-btn"));

    await waitFor(() => {
      expect(refreshNqMock).toHaveBeenCalledWith("p-1", "m-1", "nq1");
    });
    expect(screen.getByTestId("nq-refresh-queued")).toBeTruthy();
    expect(listRunsMock).toHaveBeenCalled();
  });

  it("pending-deploy indicator follows needsSaveOrDeploy", () => {
    const { unmount } = renderEditor({ mode: "edit", initial: SAMPLE_NQ });
    expect(screen.queryByTestId("nq-pending-deploy")).toBeNull();
    unmount();

    renderEditor({ mode: "edit", initial: SAMPLE_NQ, needsSaveOrDeploy: true });
    expect(screen.getByTestId("nq-pending-deploy")).toBeTruthy();
  });

  it("read-only modeller cannot author: fields disabled, no save", () => {
    renderEditor({ canEdit: false });
    expect(screen.getByLabelText("Name")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("nq-definition-sql")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("nq-save-btn")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("nq-validate-btn")).toHaveProperty("disabled", true);
  });

  it("name must match the @ reference pattern before create is enabled", async () => {
    renderEditor();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Name"), "1bad-name");
    await user.type(screen.getByTestId("nq-definition-sql"), "SELECT 1");
    expect(screen.getByTestId("nq-save-btn")).toHaveProperty("disabled", true);
  });
});
