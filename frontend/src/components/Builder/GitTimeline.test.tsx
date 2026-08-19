import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import GitTimeline from "./GitTimeline";

const gitLogMock = vi.fn();
const gitDiffMock = vi.fn();

vi.mock("../../api/versionsApi", () => ({
  versionsApi: {
    gitLog: (...args: unknown[]) => gitLogMock(...args),
    gitDiff: (...args: unknown[]) => gitDiffMock(...args),
  },
}));

vi.mock("../../i18n", () => ({
  useT: () => (key: string, vars?: Record<string, string>) => {
    if (vars) {
      let result = key;
      for (const [k, v] of Object.entries(vars)) {
        result = result.replace(`{{${k}}}`, v);
      }
      return result;
    }
    return key;
  },
}));

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const MOCK_COMMITS = [
  {
    sha: "abc1234",
    type: "model",
    message: "[model] v2: Added revenue measure",
    version: 2,
    tags: ["v2", "deploy/v2"],
    author: "admin@acme-demo.com",
    timestamp: "2026-07-17T10:00:00+00:00",
  },
  {
    sha: "def5678",
    type: "layout",
    message: "[layout] layout update",
    version: null,
    tags: [],
    author: "admin@acme-demo.com",
    timestamp: "2026-07-17T09:30:00+00:00",
  },
  {
    sha: "ghi9012",
    type: "model",
    message: "[model] v1: Initial model",
    version: 1,
    tags: ["v1"],
    author: "admin@acme-demo.com",
    timestamp: "2026-07-17T09:00:00+00:00",
  },
];

describe("GitTimeline", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders commit nodes from mock data", async () => {
    gitLogMock.mockResolvedValue(MOCK_COMMITS);
    render(<GitTimeline projectId="p1" modelId="m1" />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("v2")).toBeTruthy();
    });
    expect(screen.getByText("versions.timelineLayoutCommit")).toBeTruthy();
    expect(screen.getByText("v1")).toBeTruthy();
  });

  it("shows deploy tag badge on deployed commits", async () => {
    gitLogMock.mockResolvedValue(MOCK_COMMITS);
    render(<GitTimeline projectId="p1" modelId="m1" />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("versions.timelineDeployTag")).toBeTruthy();
    });
  });

  it("shows empty state when no commits", async () => {
    gitLogMock.mockResolvedValue([]);
    render(<GitTimeline projectId="p1" modelId="m1" />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("versions.timelineEmpty")).toBeTruthy();
    });
  });

  it("opens diff modal on View changes click", async () => {
    gitLogMock.mockResolvedValue(MOCK_COMMITS);
    gitDiffMock.mockResolvedValue("--- a/model.yaml\n+++ b/model.yaml\n@@ changed @@");

    render(<GitTimeline projectId="p1" modelId="m1" />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("v2")).toBeTruthy();
    });

    const viewButtons = screen.getAllByText("versions.timelineViewDiff");
    await userEvent.click(viewButtons[0]);

    await waitFor(() => {
      expect(gitDiffMock).toHaveBeenCalledWith("p1", "m1", "def5678", "abc1234");
    });
  });

  it("shows load more button when commits fill the page", async () => {
    const manyCommits = Array.from({ length: 50 }, (_, i) => ({
      sha: `sha${i}`,
      type: "model",
      message: `[model] v${50 - i}: commit ${i}`,
      version: 50 - i,
      tags: [],
      author: "user@test.com",
      timestamp: `2026-07-17T${String(10 + i).padStart(2, "0")}:00:00+00:00`,
    }));
    gitLogMock.mockResolvedValue(manyCommits);

    render(<GitTimeline projectId="p1" modelId="m1" />, { wrapper });

    await waitFor(() => {
      expect(screen.getByText("versions.timelineLoadMore")).toBeTruthy();
    });
  });
});
