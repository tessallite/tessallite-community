import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const driftListMock = vi.fn();
const driftAckMock = vi.fn();
const alertsListMock = vi.fn();

vi.mock("../../api/client", () => ({
  schemaDriftApi: {
    list: (...args: unknown[]) => driftListMock(...args),
    acknowledge: (...args: unknown[]) => driftAckMock(...args),
  },
  alertsApi: {
    list: (...args: unknown[]) => alertsListMock(...args),
  },
}));

import { SchemaDriftSection } from "./ModelHealthPanel";

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <SchemaDriftSection projectId="project-1" modelId="model-1" />
    </QueryClientProvider>,
  );
}

const removedEvent = {
  id: "evt-removed",
  model_id: "model-1",
  source_id: "src-1",
  table_name: "public.orders",
  change_type: "column_removed" as const,
  is_breaking: true,
  detail: { column_name: "customer_id", old_data_type: "integer" },
  detected_at: "2026-07-13T10:00:00Z",
  acknowledged_at: null,
};

const addedEventAcked = {
  id: "evt-added",
  model_id: "model-1",
  source_id: "src-1",
  table_name: "public.orders",
  change_type: "column_added" as const,
  is_breaking: false,
  detail: { column_name: "region", data_type: "text" },
  detected_at: "2026-07-12T09:00:00Z",
  acknowledged_at: "2026-07-12T11:00:00Z",
};

const impactAlert = {
  id: "alert-1",
  model_id: "model-1",
  severity: "error",
  category: "schema_drift",
  title: "Column removed: customer_id",
  detail: "Source column 'customer_id' was removed from table 'public.orders'.",
  related_object_type: "schema_change_event",
  related_object_id: "evt-removed",
  first_seen_at: "2026-07-13T10:00:01Z",
  last_seen_at: "2026-07-13T10:00:01Z",
  occurrence_count: 1,
  resolved_at: null,
  dismissed_at: null,
  dismissed_by: null,
};

describe("SchemaDriftSection", () => {
  beforeEach(() => {
    driftListMock.mockReset();
    driftAckMock.mockReset();
    alertsListMock.mockReset();
    alertsListMock.mockResolvedValue([]);
  });

  it("shows the empty state when no drift events exist", async () => {
    driftListMock.mockResolvedValue({ items: [], total: 0 });
    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/No schema drift detected/i)).toBeInTheDocument();
    });
  });

  it("renders detected events with table, column, change type and status", async () => {
    driftListMock.mockResolvedValue({
      items: [removedEvent, addedEventAcked],
      total: 2,
    });
    renderSection();

    await waitFor(() => {
      expect(screen.getAllByText("public.orders")).toHaveLength(2);
    });
    // both rows' columns render
    expect(screen.getByText("customer_id")).toBeInTheDocument();
    expect(screen.getByText("region")).toBeInTheDocument();
    // change-type labels
    expect(screen.getByText("Column removed")).toBeInTheDocument();
    expect(screen.getByText("Column added")).toBeInTheDocument();
    // status column: new (unacknowledged) + acknowledged
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(screen.getByText("Acknowledged")).toBeInTheDocument();
    // requested including acknowledged events (history)
    expect(driftListMock).toHaveBeenCalledWith("model-1", true);
  });

  it("surfaces per-event impact by joining schema_drift alerts", async () => {
    driftListMock.mockResolvedValue({ items: [removedEvent], total: 1 });
    alertsListMock.mockResolvedValue([impactAlert]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByText("Column removed: customer_id")).toBeInTheDocument();
    });
    // impact query scoped to the schema_drift category
    expect(alertsListMock).toHaveBeenCalledWith(
      "project-1",
      "model-1",
      expect.objectContaining({ category: "schema_drift" }),
    );
  });

  it("shows no-impact text when no matching alert is present", async () => {
    driftListMock.mockResolvedValue({ items: [removedEvent], total: 1 });
    alertsListMock.mockResolvedValue([]);
    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/No model objects affected/i)).toBeInTheDocument();
    });
  });

  it("calls the acknowledge endpoint for an unacknowledged event", async () => {
    driftListMock.mockResolvedValue({ items: [removedEvent], total: 1 });
    driftAckMock.mockResolvedValue({ ...removedEvent, acknowledged_at: "now" });
    renderSection();

    await waitFor(() => {
      expect(screen.getByText("customer_id")).toBeInTheDocument();
    });

    const button = screen.getByRole("button");
    fireEvent.click(button);

    await waitFor(() => {
      expect(driftAckMock).toHaveBeenCalledWith("evt-removed");
    });
  });

  it("renders an error alert when the events query fails", async () => {
    driftListMock.mockRejectedValue(new Error("boom"));
    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/Could not load schema drift events/i)).toBeInTheDocument();
    });
  });
});
