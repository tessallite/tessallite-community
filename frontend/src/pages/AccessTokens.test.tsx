import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Confirm dialog is provider-backed; auto-confirm so revoke proceeds in tests.
vi.mock("../components/Confirm", () => ({
  useConfirm: () => async () => true,
}));

const listMock = vi.fn();
const createMock = vi.fn();
const revokeMock = vi.fn();

vi.mock("../api/client", () => ({
  patApi: {
    list: () => listMock(),
    create: (d: unknown) => createMock(d),
    revoke: (id: string) => revokeMock(id),
  },
}));

import AccessTokens from "./AccessTokens";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AccessTokens />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  listMock.mockReset();
  createMock.mockReset();
  revokeMock.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("AccessTokens page (Bug-7314)", () => {
  it("lists existing tokens with masked prefix and status", async () => {
    listMock.mockResolvedValue([
      {
        id: "1",
        label: "Excel",
        token_prefix: "tesspat_abcd1234",
        created_at: "2026-07-01T00:00:00Z",
        expires_at: null,
        last_used_at: null,
        revoked_at: null,
      },
    ]);
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("Excel")).toBeInTheDocument(),
    );
    expect(screen.getByText(/tesspat_abcd1234…/)).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("shows the SSO guidance so redirect-SSO users know to use a PAT", async () => {
    listMock.mockResolvedValue([]);
    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/single sign-on/i)).toBeInTheDocument(),
    );
  });

  it("creates a token and reveals the plaintext exactly once", async () => {
    listMock.mockResolvedValue([]);
    createMock.mockResolvedValue({
      token: "tesspat_new0000_thePlaintextSecretValueShownOnce",
      pat: {
        id: "2",
        label: "New",
        token_prefix: "tesspat_new0000",
        created_at: "2026-07-10T00:00:00Z",
        expires_at: null,
        last_used_at: null,
        revoked_at: null,
      },
    });
    renderPage();
    await waitFor(() => expect(screen.getByText(/no personal access tokens/i)).toBeInTheDocument());

    // Open the generate dialog and submit.
    fireEvent.click(screen.getByRole("button", { name: /generate token/i }));
    const buttons = screen.getAllByRole("button", { name: /generate token/i });
    // The dialog's submit button is the last "Generate token" button.
    fireEvent.click(buttons[buttons.length - 1]);

    await waitFor(() =>
      expect(
        screen.getByDisplayValue(/tesspat_new0000_thePlaintextSecretValueShownOnce/),
      ).toBeInTheDocument(),
    );
    // The reveal warns this is the only time it is shown.
    expect(screen.getByText(/only time the token is shown/i)).toBeInTheDocument();
    expect(createMock).toHaveBeenCalledTimes(1);
  });

  it("revokes a token", async () => {
    listMock.mockResolvedValue([
      {
        id: "9",
        label: "Old",
        token_prefix: "tesspat_dead0000",
        created_at: "2026-07-01T00:00:00Z",
        expires_at: null,
        last_used_at: null,
        revoked_at: null,
      },
    ]);
    revokeMock.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("Old")).toBeInTheDocument());
    // The revoke icon button is the only action button in the row.
    const revokeBtn = screen.getByRole("button", { name: /revoke this token/i });
    fireEvent.click(revokeBtn);
    await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("9"));
  });
});
