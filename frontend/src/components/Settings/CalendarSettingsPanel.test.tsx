import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CalendarSettingsPanel from "./CalendarSettingsPanel";

const { getSettings, updateSettings } = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
}));
vi.mock("../../api/client", () => ({
  calendarSettingsApi: { get: getSettings, update: updateSettings },
}));

describe("CalendarSettingsPanel", () => {
  beforeEach(() => {
    localStorage.setItem("tenant_id", "tenant-a");
    getSettings.mockResolvedValue({
      format: "start_year",
      available_formats: ["start_year", "span_short", "span_long", "span_fy", "end_year"],
    });
    updateSettings.mockResolvedValue({
      format: "span_short",
      available_formats: ["start_year", "span_short", "span_long", "span_fy", "end_year"],
    });
  });

  it("BUG9487-R1-F3 lets a tenant admin select a producer-advertised format", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><CalendarSettingsPanel /></QueryClientProvider>);
    const select = await screen.findByLabelText(/fiscal year label format/i);
    await user.click(select);
    await user.click(await screen.findByRole("option", { name: "span_short" }));
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith("tenant-a", "span_short"));
    expect(await screen.findByText(/saved/i)).toBeVisible();
    expect(screen.getByText(/existing calendars use this format after their next normal rebuild/i)).toBeVisible();
  });
});
