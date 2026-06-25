import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../api/systemDefaults", () => ({
  refreshSystemDefaults: vi.fn(() => Promise.resolve()),
}));

import { refreshSystemDefaults } from "../api/systemDefaults";
import RequireAuth from "./RequireAuth";

function renderWithRouter(isAuthenticated: boolean, role?: string) {
  if (isAuthenticated) {
    Object.defineProperty(document, "cookie", {
      writable: true,
      value: "csrf_token=test-csrf",
    });
  } else {
    Object.defineProperty(document, "cookie", {
      writable: true,
      value: "",
    });
  }

  if (role) {
    localStorage.setItem("user_role", role);
  }

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
  });

  afterEach(() => {
    Object.defineProperty(document, "cookie", {
      writable: true,
      value: "",
    });
    localStorage.clear();
  });

  it("renders children when csrf_token cookie exists", () => {
    renderWithRouter(true);
    expect(screen.getByTestId("protected")).toBeInTheDocument();
  });

  it("redirects to /login when no csrf_token cookie", () => {
    renderWithRouter(false);
    expect(screen.getByTestId("login")).toBeInTheDocument();
    expect(screen.queryByTestId("protected")).not.toBeInTheDocument();
  });

  it("refreshes system defaults for system_admin role", () => {
    renderWithRouter(true, "system_admin");
    expect(refreshSystemDefaults).toHaveBeenCalledOnce();
  });

  it("does not refresh system defaults for non-admin roles", () => {
    renderWithRouter(true, "viewer");
    expect(refreshSystemDefaults).not.toHaveBeenCalled();
  });

  it("does not refresh system defaults when not authenticated", () => {
    renderWithRouter(false, "system_admin");
    expect(refreshSystemDefaults).not.toHaveBeenCalled();
  });
});
