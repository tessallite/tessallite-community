// Bug-9324: the revoke-failure alert must use its own i18n key — never the
// load-failed string, never raw English.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { embedTokensApi } from "../../api/client";
import EmbedTokensPanel from "./EmbedTokensPanel";

vi.mock("../../api/client", () => ({
  embedTokensApi: {
    list: vi.fn(),
    revoke: vi.fn(),
  },
}));

const TOKEN = {
  jti: "jti-1",
  user_identity: "analyst@example.com",
  expires_at: null,
  revoked_at: null,
};

function renderPanel(messages: Record<string, string>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nContext.Provider value={messages}>
        <EmbedTokensPanel />
      </I18nContext.Provider>
    </QueryClientProvider>,
  );
}

describe("EmbedTokensPanel revoke-failure copy (Bug-9324)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (embedTokensApi.list as ReturnType<typeof vi.fn>).mockResolvedValue([TOKEN]);
  });

  it("shows the dedicated revoke-failure message when revoke errors", async () => {
    (embedTokensApi.revoke as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("boom"),
    );
    renderPanel(en as Record<string, string>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(
        screen.getByText("Could not revoke the embed token."),
      ).toBeInTheDocument(),
    );
    // The load-failed copy must NOT be reused for a revoke failure.
    expect(
      screen.queryByText("Could not load embed tokens."),
    ).not.toBeInTheDocument();
  });

  it("still uses the load-failed key for a list failure", async () => {
    (embedTokensApi.list as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("boom"),
    );
    renderPanel(en as Record<string, string>);
    expect(
      await screen.findByText("Could not load embed tokens."),
    ).toBeInTheDocument();
  });

  it("renders revoke-failure copy through the translator, never raw English", async () => {
    const marked: Record<string, string> = {};
    for (const key of Object.keys(en as Record<string, string>)) {
      marked[key] = `‹${key}›`;
    }
    (embedTokensApi.revoke as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("boom"),
    );
    renderPanel(marked);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "‹embedTokens.revoke›" }),
    );
    expect(
      await screen.findByText("‹embedTokens.revokeFailed›"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Could not revoke the embed token.")).toBeNull();
  });
});
