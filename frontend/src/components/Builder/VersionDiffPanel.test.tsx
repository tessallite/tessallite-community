import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Bug-5916: version diff must surface singleton/dict snapshot categories
// (model, model_alias_map, refresh_sla_config, ai_scheduler_config,
// model_settings), not just the list-shaped categories.
const diffMock = vi.fn();

vi.mock("../../api/client", () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

vi.mock("../../api/versionsApi", async () => {
  const actual = await vi.importActual<typeof import("../../api/versionsApi")>(
    "../../api/versionsApi",
  );
  return {
    ...actual,
    useVersionDiff: (...args: unknown[]) => diffMock(...args),
  };
});

import VersionDiffPanel from "./VersionDiffPanel";

const versions = [
  { id: "v2", version_number: 2, summary: null, created_at: "", created_by: "", is_deployed: true },
  { id: "v1", version_number: 1, summary: null, created_at: "", created_by: "", is_deployed: false },
];

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <VersionDiffPanel projectId="proj-1" modelId="model-1" versions={versions} />
    </QueryClientProvider>,
  );
}

describe("VersionDiffPanel", () => {
  beforeEach(() => {
    diffMock.mockReset();
  });

  it("renders a singleton category field change (model_settings)", async () => {
    diffMock.mockReturnValue({
      isLoading: false,
      isError: false,
      data: {
        version_a: 1,
        version_b: 2,
        diff: {
          tables: { added: [], removed: [], changed: [] },
          model_settings: {
            changes: {
              query_cache_ttl_seconds: { from: 300, to: 900 },
            },
          },
        },
      },
    });
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Model Settings")).toBeTruthy();
    });
    expect(screen.getByText("query_cache_ttl_seconds")).toBeTruthy();
    expect(screen.getByText("300")).toBeTruthy();
    expect(screen.getByText("900")).toBeTruthy();
  });

  it("does not render a singleton category with no changes", async () => {
    diffMock.mockReturnValue({
      isLoading: false,
      isError: false,
      data: {
        version_a: 1,
        version_b: 2,
        diff: {
          model: { changes: {} },
        },
      },
    });
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/no differences/i)).toBeTruthy();
    });
    expect(screen.queryByText("Model")).toBeNull();
  });

  it("counts singleton field changes in the total change chip", async () => {
    diffMock.mockReturnValue({
      isLoading: false,
      isError: false,
      data: {
        version_a: 1,
        version_b: 2,
        diff: {
          model_alias_map: {
            changes: { warehouse: { from: "wh1", to: "wh2" } },
          },
        },
      },
    });
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("1 change")).toBeTruthy();
    });
  });
});
