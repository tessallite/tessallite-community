import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../api/systemDefaults", () => ({
  refreshSystemDefaults: vi.fn(() => Promise.resolve()),
}));

// Bug-7318: RequireAuth now validates the session server-side instead of
// trusting cookie presence. A tenant session is validated via authApi.me(); a
// system-admin session (role=system_admin, no tenant_id) is validated via
// systemSettingsApi.list() because /users/me is tenant-scoped and 500s for the
// __system__ session. Mock both so each test controls whether the backend
// accepts the session.
vi.mock("../api/client", () => ({
  authApi: {
    me: vi.fn(() => Promise.resolve({ id: "u1", role: "viewer" })),
  },
  systemSettingsApi: {
    list: vi.fn(() => Promise.resolve([])),
  },
}));

import { refreshSystemDefaults } from "../api/systemDefaults";
import { authApi, systemSettingsApi } from "../api/client";
import RequireAuth from "./RequireAuth";

const meMock = authApi.me as unknown as ReturnType<typeof vi.fn>;
const systemListMock = systemSettingsApi.list as unknown as ReturnType<
  typeof vi.fn
>;

function setCookie(value: string) {
  Object.defineProperty(document, "cookie", { writable: true, value });
}

function renderWithRouter(cookiePresent: boolean, role?: string, tenantId?: string) {
  setCookie(cookiePresent ? "csrf_token=test-csrf" : "");
  if (role) localStorage.setItem("user_role", role);
  if (tenantId) localStorage.setItem("tenant_id", tenantId);

  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <Routes>
        <Route
          path="/dashboard"
          element={
            <RequireAuth>
              <div data-testid="protected">Protected Content</div>
            </RequireAuth>
          }
        />
        <Route path="/login" element={<div data-testid="login">Login Page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RequireAuth", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    meMock.mockResolvedValue({ id: "u1", role: "viewer" });
    systemListMock.mockResolvedValue([]);
  });

  afterEach(() => {
    setCookie("");
    localStorage.clear();
  });

  it("renders children only after the server confirms the session", async () => {
    renderWithRouter(true);
    // Fail-closed: nothing protected is shown until /users/me resolves.
    expect(screen.queryByTestId("protected")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("protected")).toBeInTheDocument(),
    );
    expect(meMock).toHaveBeenCalledOnce();
  });

  it("redirects to /login when no csrf_token cookie (never calls the server)", async () => {
    renderWithRouter(false);
    await waitFor(() =>
      expect(screen.getByTestId("login")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("protected")).not.toBeInTheDocument();
    expect(meMock).not.toHaveBeenCalled();
  });

  it("redirects to /login when a forged cookie fails server validation", async () => {
    // A cookie can be forged (not HttpOnly); the server rejects the session.
    meMock.mockRejectedValue({ response: { status: 401 } });
    renderWithRouter(true);
    await waitFor(() =>
      expect(screen.getByTestId("login")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("protected")).not.toBeInTheDocument();
    expect(meMock).toHaveBeenCalledOnce();
  });

  // Bug-7318 regression: a system-admin session (no tenant_id) must be admitted
  // via the system probe, NOT /users/me — /users/me is tenant-scoped and 500s
  // for the __system__ session, which would lock every system admin out.
  it("validates a system-admin session via the system endpoint, not /users/me", async () => {
    renderWithRouter(true, "system_admin");
    await waitFor(() =>
      expect(screen.getByTestId("protected")).toBeInTheDocument(),
    );
    expect(systemListMock).toHaveBeenCalledOnce();
    expect(meMock).not.toHaveBeenCalled();
  });

  it("fails closed when the system-admin session probe is rejected", async () => {
    // A forged cookie + forged localStorage role hits /system/settings
    // (require_system_admin); the server rejects it, so the guard redirects.
    systemListMock.mockRejectedValue({ response: { status: 401 } });
    renderWithRouter(true, "system_admin");
    await waitFor(() =>
      expect(screen.getByTestId("login")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("protected")).not.toBeInTheDocument();
  });

  it("uses /users/me (not the system probe) for a system_admin who still has a tenant_id", async () => {
    // A tenant-scoped user whose role happens to be system_admin but who has a
    // stored tenant_id is a tenant session; validate via /users/me.
    renderWithRouter(true, "system_admin", "acme-demo");
    await waitFor(() =>
      expect(screen.getByTestId("protected")).toBeInTheDocument(),
    );
    expect(meMock).toHaveBeenCalledOnce();
    expect(systemListMock).not.toHaveBeenCalled();
  });

  it("refreshes system defaults for system_admin role after validation", async () => {
    renderWithRouter(true, "system_admin");
    await waitFor(() =>
      expect(refreshSystemDefaults).toHaveBeenCalledOnce(),
    );
  });

  it("does not refresh system defaults for non-admin roles", async () => {
    renderWithRouter(true, "viewer");
    await waitFor(() =>
      expect(screen.getByTestId("protected")).toBeInTheDocument(),
    );
    expect(refreshSystemDefaults).not.toHaveBeenCalled();
  });

  it("does not refresh system defaults when not authenticated", async () => {
    renderWithRouter(false, "system_admin");
    await waitFor(() =>
      expect(screen.getByTestId("login")).toBeInTheDocument(),
    );
    expect(refreshSystemDefaults).not.toHaveBeenCalled();
  });
});
