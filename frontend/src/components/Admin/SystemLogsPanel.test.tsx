import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

const settingsMock = vi.fn();
const listMock = vi.fn();
const purgeMock = vi.fn();

vi.mock("../../api/client", () => ({
  systemLogsApi: {
    settings: (...args: unknown[]) => settingsMock(...args),
    list: (...args: unknown[]) => listMock(...args),
    purge: (...args: unknown[]) => purgeMock(...args),
  },
}));

vi.mock("../../i18n", () => ({
  useT: () => (key: string, vars?: Record<string, string | number>) => {
    const messages: Record<string, string> = {
      "systemLogs.allLevels": "All levels",
      "systemLogs.allServices": "All services",
      "systemLogs.cancel": "Cancel",
      "systemLogs.confirmPurge": "Purge expired logs",
      "systemLogs.cutoff": "Retention cutoff:",
      "systemLogs.details": "Details",
      "systemLogs.disabled": "System log capture is disabled.",
      "systemLogs.empty": "No system logs match the current filters.",
      "systemLogs.from": "From",
      "systemLogs.hideDetails": "Hide details",
      "systemLogs.id": "ID",
      "systemLogs.instance": "Instance",
      "systemLogs.level": "Severity",
      "systemLogs.levelCritical": "Critical",
      "systemLogs.levelDebug": "Debug",
      "systemLogs.levelError": "Error",
      "systemLogs.levelInfo": "Info",
      "systemLogs.levelWarning": "Warning",
      "systemLogs.loadFailed": "System logs could not be loaded.",
      "systemLogs.loadOlder": "Load older",
      "systemLogs.loading": "Loading system logs…",
      "systemLogs.logger": "Logger",
      "systemLogs.message": "Message",
      "systemLogs.pause": "Pause",
      "systemLogs.pollingStatus": "Live updates every {{seconds}}s",
      "systemLogs.purgeExpired": "Purge expired",
      "systemLogs.purgeFailed": "Expired logs could not be purged. Refresh and try again.",
      "systemLogs.purgeMessage": "This permanently removes records older than the retention cutoff.",
      "systemLogs.purgeSuccess": "Expired log purge complete: {{count}} records deleted; retention cutoff: {{cutoff}}.",
      "systemLogs.purgeTitle": "Purge expired system logs?",
      "systemLogs.rawDescription": "Raw runtime output from configured services.",
      "systemLogs.refresh": "Refresh system logs",
      "systemLogs.retentionDisabled": "Expiry-based retention is disabled. Purge is unavailable.",
      "systemLogs.resume": "Resume",
      "systemLogs.retry": "Retry",
      "systemLogs.rowsShown": "{{count}} rows shown",
      "systemLogs.search": "Search message, logger or instance",
      "systemLogs.service": "Service",
      "systemLogs.timestamp": "Timestamp",
      "systemLogs.to": "To",
      "systemLogs.unknownCutoff": "Unavailable",
      "systemLogs.unknownLevel": "Unknown",
      "systemLogs.title": "System logs",
    };
    return (messages[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_match, name: string) =>
      String(vars?.[name] ?? `{{${name}}}`),
    );
  },
}));

import SystemLogsPanel from "./SystemLogsPanel";

const settings = {
  enabled: true,
  retention_days: 30,
  cutoff: "2026-08-01T00:00:00+00:00",
  levels: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
  services: ["gateway", "frontend", "postgres"],
  poll_seconds: 5,
  page_size: 50,
};

const firstRow = {
  id: "00000000-0000-0000-0000-000000000001",
  timestamp: "2026-09-10T10:00:00+00:00",
  service: "gateway",
  level: "ERROR",
  logger: "gateway.request",
  instance: "gateway-1",
  message: "connection timed out\nretry scheduled",
};

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SystemLogsPanel />
    </QueryClientProvider>,
  );
}

function findRawMessage(message: string) {
  return screen.findByText((_content, element) =>
    element?.tagName === "PRE" && element.textContent === message,
  );
}

beforeEach(() => {
  settingsMock.mockReset();
  listMock.mockReset();
  purgeMock.mockReset();
  localStorage.clear();
  localStorage.setItem("user_role", "system_admin");
  settingsMock.mockResolvedValue(settings);
  listMock.mockResolvedValue({ items: [], next_cursor: null });
  purgeMock.mockResolvedValue({ deleted: 0, cutoff: settings.cutoff });
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe("SystemLogsPanel", () => {
  it("keeps the panel and its API calls behind the system-admin route guard", () => {
    localStorage.removeItem("user_role");

    const { container } = renderPanel();

    expect(container.firstChild).toBeNull();
    expect(settingsMock).not.toHaveBeenCalled();
    expect(listMock).not.toHaveBeenCalled();
  });

  it("renders raw messages, severity labels and expandable details", async () => {
    listMock.mockResolvedValue({ items: [firstRow], next_cursor: null });

    renderPanel();

    expect(await findRawMessage(firstRow.message)).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("gateway.request")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Details" }));

    expect(screen.getByText(firstRow.id)).toBeInTheDocument();
    expect(screen.getAllByText(firstRow.timestamp)).toHaveLength(1);
  });

  it("sends search, service, severity and timezone-aware time filters", async () => {
    listMock.mockResolvedValue({ items: [], next_cursor: null });
    renderPanel();
    await screen.findByText("No system logs match the current filters.");

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Search message, logger or instance"), "timeout");
    await user.click(screen.getByRole("combobox", { name: "Service" }));
    await user.click(screen.getByRole("option", { name: "gateway" }));
    await user.click(screen.getByRole("combobox", { name: "Severity" }));
    await user.click(screen.getByRole("option", { name: "Error" }));
    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "2026-09-10T08:30" },
    });
    fireEvent.change(screen.getByLabelText("To"), {
      target: { value: "2026-09-10T18:30" },
    });

    await waitFor(() => {
      expect(listMock).toHaveBeenLastCalledWith({
        q: "timeout",
        service: "gateway",
        level: "ERROR",
        from_date: new Date("2026-09-10T08:30").toISOString(),
        to_date: new Date("2026-09-10T18:30").toISOString(),
        cursor: undefined,
        limit: 50,
      });
    });
  });

  it("loads older rows with the opaque cursor returned by the API", async () => {
    const olderRow = { ...firstRow, id: "00000000-0000-0000-0000-000000000002", message: "older row" };
    listMock
      .mockResolvedValueOnce({ items: [firstRow], next_cursor: "opaque-cursor" })
      .mockResolvedValueOnce({ items: [olderRow], next_cursor: null });

    renderPanel();
    expect(await findRawMessage(firstRow.message)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Load older" }));

    await waitFor(() => expect(screen.getByText("older row")).toBeInTheDocument());
    expect(listMock).toHaveBeenLastCalledWith(expect.objectContaining({
      cursor: "opaque-cursor",
      limit: 50,
    }));
  });

  it("pauses and resumes the configured live polling control", async () => {
    vi.useFakeTimers();
    renderPanel();
    try {
      for (let attempt = 0; attempt < 10 && listMock.mock.calls.length === 0; attempt += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
          await Promise.resolve();
        });
      }
      expect(listMock).toHaveBeenCalledTimes(1);
      for (let attempt = 0; attempt < 10 && !screen.queryByText("No system logs match the current filters."); attempt += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
          await Promise.resolve();
        });
      }
      expect(screen.getByText("No system logs match the current filters.")).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(settings.poll_seconds * 1000);
      });
      expect(listMock).toHaveBeenCalledTimes(2);

      fireEvent.click(screen.getByRole("button", { name: "Pause" }));
      expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(settings.poll_seconds * 2 * 1000);
      });
      expect(listMock).toHaveBeenCalledTimes(2);

      fireEvent.click(screen.getByRole("button", { name: "Resume" }));
      expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(settings.poll_seconds * 1000);
      });
      expect(listMock).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps historical rows readable when collection is disabled", async () => {
    settingsMock.mockResolvedValue({ ...settings, enabled: false });
    listMock.mockResolvedValue({ items: [firstRow], next_cursor: null });

    renderPanel();

    expect(await findRawMessage(firstRow.message)).toBeInTheDocument();
    expect(screen.getByText("System log capture is disabled.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(listMock).toHaveBeenCalledWith(expect.objectContaining({ limit: 50, cursor: undefined }));
  });

  it("shows the retention cutoff before an admin purge and refreshes after success", async () => {
    purgeMock.mockResolvedValue({ deleted: 7, cutoff: settings.cutoff });
    renderPanel();
    await screen.findByText("No system logs match the current filters.");

    await userEvent.click(screen.getByRole("button", { name: "Purge expired" }));
    expect(screen.getByTestId("system-logs-purge-cutoff")).toHaveTextContent(settings.cutoff);

    await userEvent.click(screen.getByRole("button", { name: "Purge expired logs" }));

    await waitFor(() => expect(purgeMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText("Purge expired system logs?")).not.toBeInTheDocument());
    expect(listMock).toHaveBeenCalledTimes(2);
    const successMessage = "Expired log purge complete: 7 records deleted; retention cutoff: 2026-08-01T00:00:00+00:00.";
    expect(await screen.findByText(successMessage)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Purge expired" }));
    expect(screen.queryByText(successMessage)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  });

  it("renders a nullable cutoff from the authoritative purge response", async () => {
    purgeMock.mockResolvedValue({ deleted: 3, cutoff: null });
    renderPanel();
    await screen.findByText("No system logs match the current filters.");

    await userEvent.click(screen.getByRole("button", { name: "Purge expired" }));
    await userEvent.click(screen.getByRole("button", { name: "Purge expired logs" }));

    expect(await screen.findByText("Expired log purge complete: 3 records deleted; retention cutoff: Unavailable.")).toBeInTheDocument();
  });

  it("keeps expiry purge unavailable when retention is disabled", async () => {
    settingsMock.mockResolvedValue({ ...settings, retention_days: 0, cutoff: null });

    renderPanel();

    expect(await screen.findByText("Expiry-based retention is disabled. Purge is unavailable.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Purge expired" })).toBeDisabled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(purgeMock).not.toHaveBeenCalled();
  });

  it("reports a list API failure instead of showing a false empty state", async () => {
    listMock.mockRejectedValue(new Error("request failed"));

    renderPanel();

    expect(await screen.findByText("System logs could not be loaded.")).toBeInTheDocument();
    expect(screen.queryByText("No system logs match the current filters.")).not.toBeInTheDocument();
  });

  it("keeps a purge API failure visible in the confirmation dialog", async () => {
    purgeMock.mockRejectedValue(new Error("purge failed"));
    renderPanel();
    await screen.findByText("No system logs match the current filters.");

    await userEvent.click(screen.getByRole("button", { name: "Purge expired" }));
    await userEvent.click(screen.getByRole("button", { name: "Purge expired logs" }));

    expect(await screen.findByText("Expired logs could not be purged. Refresh and try again.")).toBeInTheDocument();
    expect(screen.getByTestId("system-logs-purge-cutoff")).toHaveTextContent(settings.cutoff);
  });
});
