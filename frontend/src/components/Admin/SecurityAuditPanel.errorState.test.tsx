/**
 * Bug-8145 — a failed security-audit request must render a distinct error
 * state with retry, not fall through to the empty "no queries with row
 * security" message. Before this fix, `SecurityAuditPanel` only branched on
 * `isLoading` and `items.length === 0`; an API failure produced `data ===
 * undefined`, `items` defaulted to `[]`, and the panel silently told an
 * administrator "no row-security queries" during an outage or auth failure —
 * the exact class of defect fixed for AuditLog.tsx under the same finding
 * (F-022-09).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const listMock = vi.fn();

vi.mock("../../api/client", () => ({
  securityAuditApi: {
    list: (...a: unknown[]) => listMock(...a),
  },
}));

vi.mock("../../i18n", () => ({ useT: () => (key: string) => key }));

import SecurityAuditPanel from "./SecurityAuditPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SecurityAuditPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("SecurityAuditPanel — API failure state (Bug-8145)", () => {
  it("renders a translated error state with retry, not the empty state, on API failure", async () => {
    listMock.mockRejectedValue(new Error("boom"));
    renderPanel();

    await waitFor(() =>
      expect(screen.getByText("audit.apiFailure")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText("audit.noQueriesWithRowSecurity"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("auditLog.retry")).toBeInTheDocument();
  });

  it("retries the query when the retry action is clicked", async () => {
    listMock.mockRejectedValueOnce(new Error("boom"));
    listMock.mockResolvedValueOnce({ items: [], total: 0 });
    renderPanel();

    await waitFor(() =>
      expect(screen.getByText("audit.apiFailure")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByText("auditLog.retry"));

    await waitFor(() =>
      expect(screen.getByText("audit.noQueriesWithRowSecurity")).toBeInTheDocument(),
    );
    expect(listMock).toHaveBeenCalledTimes(2);
  });

  it("still shows the empty state (not an error) for a genuine zero-item response", async () => {
    listMock.mockResolvedValue({ items: [], total: 0 });
    renderPanel();

    await waitFor(() =>
      expect(
        screen.getByText("audit.noQueriesWithRowSecurity"),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("audit.apiFailure")).not.toBeInTheDocument();
  });
});
