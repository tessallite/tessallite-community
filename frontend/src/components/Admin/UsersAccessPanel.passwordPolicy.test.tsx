/**
 * Bug-8184 — the new-user form states the password rule and refuses to submit
 * a password the API would reject.
 *
 * Before this, the form asked for a password, said nothing about the rule, and
 * happily enabled Save; the operator learned the requirement from a 422 after
 * the round trip. This proves the rule is WIRED into this form, not merely
 * defined in `auth/passwordPolicy.ts` (which its own test covers).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const listTenantUsersMock = vi.fn();
const createTenantUserMock = vi.fn();
const accessListMock = vi.fn();
const modelsListMock = vi.fn();

vi.mock("../../api/client", () => ({
  authApi: {
    listTenantUsers: (...a: unknown[]) => listTenantUsersMock(...a),
    createTenantUser: (...a: unknown[]) => createTenantUserMock(...a),
    updateTenantUser: vi.fn(),
    deleteTenantUser: vi.fn(),
    resetTenantUserPassword: vi.fn(),
  },
  accessApi: {
    list: (...a: unknown[]) => accessListMock(...a),
    revoke: vi.fn(),
  },
  modelsApi: {
    list: (...a: unknown[]) => modelsListMock(...a),
  },
}));

vi.mock("../../i18n", () => ({ useT: () => (key: string) => key }));
vi.mock("../Confirm", () => ({ useConfirm: () => vi.fn().mockResolvedValue(true) }));
vi.mock("../Settings/EffectiveAccessPreview", () => ({ default: () => null }));
vi.mock("./grantAccessWithSupersede", () => ({
  grantAccessWithSupersede: vi.fn(),
}));

import UsersAccessPanel from "./UsersAccessPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <UsersAccessPanel projectId="p1" projectName="Project One" />
    </QueryClientProvider>,
  );
}

async function openNewUserDrawer() {
  renderPanel();
  await waitFor(() => expect(screen.getByText("users.newUser")).toBeInTheDocument());
  await userEvent.click(screen.getByText("users.newUser"));
  await userEvent.type(screen.getByLabelText("users.emailLabel"), "new@acme-demo.com");
  await userEvent.type(screen.getByLabelText("users.usernameLabel"), "newuser");
}

function saveButton() {
  return screen.getByText("users.saveButton").closest("button") as HTMLButtonElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  listTenantUsersMock.mockResolvedValue([]);
  accessListMock.mockResolvedValue([]);
  modelsListMock.mockResolvedValue([]);
  createTenantUserMock.mockResolvedValue({});
});

describe("Bug-8184 — the password rule is stated and enforced in the form", () => {
  it("states the rule before anything is typed", async () => {
    await openNewUserDrawer();
    expect(screen.getByText("errors.form.passwordComplexity")).toBeInTheDocument();
  });

  it("refuses to submit a password the API would reject", async () => {
    await openNewUserDrawer();

    // Long enough, but no digit — one of the four ValueErrors the server
    // raises in shared/schemas/domains/auth.py.
    await userEvent.type(screen.getByLabelText("users.passwordLabel"), "Abcdefgh");

    // Disabled AND flagged: the operator is told the field is the problem
    // rather than left wondering why Save does nothing.
    expect(saveButton()).toBeDisabled();
    expect(screen.getByLabelText("users.passwordLabel")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(createTenantUserMock).not.toHaveBeenCalled();
  });

  it("submits once the password satisfies the rule", async () => {
    await openNewUserDrawer();
    await userEvent.type(screen.getByLabelText("users.passwordLabel"), "Ab1defghijkl");

    await waitFor(() => expect(saveButton()).not.toBeDisabled());
    await userEvent.click(saveButton());

    await waitFor(() =>
      expect(createTenantUserMock).toHaveBeenCalledWith("", {
        email: "new@acme-demo.com",
        username: "newuser",
        password: "Ab1defghijkl",
        role: "member",
      }),
    );
  });
});
