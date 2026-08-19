import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfirmContext } from "../Confirm/useConfirm";

// Bug-6642: the admin Users table must surface `role_source` so an operator can
// distinguish an SSO-elevated admin from a manually-promoted one.

const listTenantUsersMock = vi.fn();

vi.mock("../../api/client", () => ({
  authApi: {
    listTenantUsers: (...args: unknown[]) => listTenantUsersMock(...args),
    createTenantUser: vi.fn(),
    updateTenantUser: vi.fn(),
    deleteTenantUser: vi.fn(),
    resetTenantUserPassword: vi.fn(),
  },
  accessApi: {
    list: vi.fn().mockResolvedValue([]),
    grant: vi.fn(),
    revoke: vi.fn(),
  },
  modelsApi: {
    list: vi.fn().mockResolvedValue([]),
  },
}));

import UsersAccessPanel from "./UsersAccessPanel";

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const confirm = vi.fn().mockResolvedValue(true);
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmContext.Provider value={confirm}>
        <UsersAccessPanel projectId="p1" projectName="Project One" />
      </ConfirmContext.Provider>
    </QueryClientProvider>,
  );
}

function baseUser(overrides: Record<string, unknown>) {
  return {
    id: "u-0",
    username: "user",
    email: "user@acme.test",
    is_active: true,
    role: "tenant_admin",
    has_completed_onboarding: true,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("UsersAccessPanel role_source badge (Bug-6642)", () => {
  beforeEach(() => {
    listTenantUsersMock.mockReset();
  });

  it("renders an SSO badge for an SSO-provenance user", async () => {
    listTenantUsersMock.mockResolvedValue([
      baseUser({
        id: "u-sso",
        email: "sso-admin@acme.test",
        role_source: "sso",
      }),
    ]);

    renderPanel();

    const cell = await screen.findByText("sso-admin@acme.test");
    const row = cell.closest("tr")!;
    const badge = within(row).getByText("SSO");
    expect(badge).toBeInTheDocument();
    expect(within(row).queryByText("Manual")).not.toBeInTheDocument();

    // The tooltip carries the operator-facing provenance explanation. Guard
    // against the two tooltip keys being swapped by asserting the rendered text.
    await userEvent.hover(badge);
    expect(
      await screen.findByText(/granted automatically from an SSO/i),
    ).toBeInTheDocument();
  });

  it("renders a Manual badge for a manual-provenance user", async () => {
    listTenantUsersMock.mockResolvedValue([
      baseUser({
        id: "u-manual",
        email: "manual-admin@acme.test",
        role_source: "manual",
      }),
    ]);

    renderPanel();

    const cell = await screen.findByText("manual-admin@acme.test");
    const row = cell.closest("tr")!;
    expect(within(row).getByText("Manual")).toBeInTheDocument();
    expect(within(row).queryByText("SSO")).not.toBeInTheDocument();
  });

  it("defaults to Manual when role_source is absent", async () => {
    listTenantUsersMock.mockResolvedValue([
      baseUser({ id: "u-legacy", email: "legacy@acme.test" }),
    ]);

    renderPanel();

    const cell = await screen.findByText("legacy@acme.test");
    const row = cell.closest("tr")!;
    expect(within(row).getByText("Manual")).toBeInTheDocument();
  });

  it("shows the Source column header", async () => {
    listTenantUsersMock.mockResolvedValue([
      baseUser({ id: "u-h", email: "h@acme.test", role_source: "sso" }),
    ]);

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("Source")).toBeInTheDocument();
    });
  });
});
