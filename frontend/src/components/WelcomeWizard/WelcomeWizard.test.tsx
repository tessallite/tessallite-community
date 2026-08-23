import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

vi.mock("../../api/client", () => ({
  authApi: {
    completeOnboarding: vi.fn().mockResolvedValue({}),
  },
}));

import WelcomeWizard from "./WelcomeWizard";

function renderWizard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/welcome"]}>
        <WelcomeWizard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WelcomeWizard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the Getting Started heading", () => {
    renderWizard();
    expect(screen.getByText("Getting Started")).toBeInTheDocument();
  });

  it("shows all step labels", () => {
    renderWizard();
    expect(screen.getByText("Welcome")).toBeInTheDocument();
    expect(screen.getByText("Add Sources")).toBeInTheDocument();
    expect(screen.getByText("Build Model")).toBeInTheDocument();
    expect(screen.getByText("Run Query")).toBeInTheDocument();
    expect(screen.getByText("Connect BI Tool")).toBeInTheDocument();
  });

  it("shows WelcomeStep content on the first step", () => {
    renderWizard();
    expect(screen.getByText("Welcome to Tessallite")).toBeInTheDocument();
  });

  it("navigates to next step when Next is clicked", async () => {
    const user = userEvent.setup();
    renderWizard();
    await user.click(screen.getByRole("button", { name: /^next$/i }));
    expect(screen.getByText("Add Data Sources")).toBeInTheDocument();
  });

  it("shows Skip button on non-final steps", () => {
    renderWizard();
    expect(screen.getByRole("button", { name: /^skip$/i })).toBeInTheDocument();
  });

  it("shows Skip all link", () => {
    renderWizard();
    expect(screen.getByText(/skip all/i)).toBeInTheDocument();
  });

  it("shows Finish button on the last step", async () => {
    const user = userEvent.setup();
    renderWizard();
    for (let i = 0; i < 4; i++) {
      await user.click(screen.getByRole("button", { name: /^next$/i }));
    }
    expect(screen.getByRole("button", { name: /^finish$/i })).toBeInTheDocument();
  });

  // Bug-7447: completing onboarding failure must show error, not trap user.
  it("shows error and re-enables controls when completeOnboarding fails", async () => {
    const { authApi } = await import("../../api/client");
    (authApi.completeOnboarding as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("network error"),
    );

    const user = userEvent.setup();
    renderWizard();
    // Click Skip All to trigger the completion
    await user.click(screen.getByText(/skip all/i));
    // Error message should be displayed
    expect(
      await screen.findByText("Could not complete setup. Please try again."),
    ).toBeInTheDocument();
    // The Skip All link should be re-enabled (completing=false)
    expect(screen.getByText(/skip all/i)).not.toBeDisabled();
  });
});
