// Bug-9317: the git-settings save-failure alert was the last hardcoded
// English literal in the panel; it must render through the i18n catalogue.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import api from "../../api/client";
import GitSettingsPanel from "./GitSettingsPanel";

vi.mock("../../api/client", () => ({
  default: {
    get: vi.fn(),
    put: vi.fn(),
    post: vi.fn(),
  },
}));

function renderPanel(messages: Record<string, string>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nContext.Provider value={messages}>
        <GitSettingsPanel />
      </I18nContext.Provider>
    </QueryClientProvider>,
  );
}

describe("GitSettingsPanel save-error copy (Bug-9317)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { remote_url: "https://github.com/org/repo.git", has_token: true },
    });
    (api.put as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"));
  });

  it("shows the translated save-failure copy when save errors", async () => {
    renderPanel(en as Record<string, string>);
    const user = userEvent.setup();
    const url = await screen.findByLabelText("Remote repository URL");
    await user.clear(url);
    await user.type(url, "https://github.com/org/other.git");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(screen.getByText("Save failed.")).toBeInTheDocument(),
    );
  });

  it("routes the save-failure copy through the translator, never raw English", async () => {
    const marked: Record<string, string> = {};
    for (const key of Object.keys(en as Record<string, string>)) {
      marked[key] = `‹${key}›`;
    }
    renderPanel(marked);
    const user = userEvent.setup();
    const url = await screen.findByLabelText("‹git.remoteUrl›");
    await user.clear(url);
    await user.type(url, "https://github.com/org/other.git");
    await user.click(screen.getByRole("button", { name: "‹git.save›" }));
    expect(await screen.findByText("‹git.saveFailed›")).toBeInTheDocument();
    expect(screen.queryByText("Save failed.")).toBeNull();
  });
});
