/**
 * An audit log that cannot be read must not look like an audit log with
 * nothing in it.
 *
 * When the request failed, this page fell straight through to the empty state —
 * "No audit events found." A compliance reviewer looking for a specific action
 * was told the opposite of the truth, with no indication anything had gone
 * wrong and no way to try again short of reloading the browser.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const listMock = vi.fn();
const exportCsvMock = vi.fn();
const listActionsMock = vi.fn();

vi.mock("../api/client", () => ({
  auditApi: {
    list: (...args: unknown[]) => listMock(...args),
    exportCsv: (...args: unknown[]) => exportCsvMock(...args),
    listActions: (...args: unknown[]) => listActionsMock(...args),
  },
}));

vi.mock("../i18n", () => ({
  useT: () => (key: string) => key,
}));

import AuditLog from "./AuditLog";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuditLog />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listActionsMock.mockResolvedValue([]);
});

describe("AuditLog — a failed load is reported, not disguised", () => {
  it("shows the failure instead of the empty state", async () => {
    listMock.mockRejectedValue(new Error("500"));

    renderPage();

    await waitFor(() =>
      expect(screen.getByText("auditLog.loadFailed")).toBeInTheDocument(),
    );
    expect(screen.queryByText("auditLog.noEvents")).not.toBeInTheDocument();
  });

  it("keeps the empty state for a genuinely empty log", async () => {
    listMock.mockResolvedValue({ items: [], total: 0 });

    renderPage();

    await waitFor(() =>
      expect(screen.getByText("auditLog.noEvents")).toBeInTheDocument(),
    );
    expect(screen.queryByText("auditLog.loadFailed")).not.toBeInTheDocument();
  });

  it("retries the request and renders the events when it succeeds", async () => {
    listMock.mockRejectedValueOnce(new Error("500")).mockResolvedValue({
      items: [
        {
          id: "e1",
          timestamp: "2026-08-11T10:00:00Z",
          actor: "admin@acme-demo.com",
          action: "model.deploy",
          target: "modelx",
          severity: "info",
          ip_address: "10.0.0.1",
          details: null,
        },
      ],
      total: 1,
    });

    renderPage();

    await waitFor(() =>
      expect(screen.getByText("auditLog.loadFailed")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByText("auditLog.retry"));

    await waitFor(() =>
      expect(screen.getByText("model.deploy")).toBeInTheDocument(),
    );
    expect(screen.queryByText("auditLog.loadFailed")).not.toBeInTheDocument();
    expect(listMock).toHaveBeenCalledTimes(2);
  });
});
