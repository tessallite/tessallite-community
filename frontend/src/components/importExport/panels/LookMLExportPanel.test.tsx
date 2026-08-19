import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import LookMLExportPanel from "./LookMLExportPanel";
import { modelsApi } from "../../../api/client";
import { lookmlExportApi } from "../../../api/importExportApi";

// F-020-04: LookML export skips calculated / time-variant measures; the panel
// must surface the skipped-measure count and NOT auto-close when > 0.
vi.mock("../../../api/client", () => ({
  modelsApi: { list: vi.fn() },
}));

vi.mock("../../../api/importExportApi", () => ({
  lookmlExportApi: { exportModel: vi.fn() },
}));

vi.mock("../../../i18n", () => ({
  useT: () => (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}:${JSON.stringify(vars)}` : key,
}));

const listModels = modelsApi.list as ReturnType<typeof vi.fn>;
const exportModel = lookmlExportApi.exportModel as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  listModels.mockResolvedValue([{ id: "m1", slug: "sales", display_name: "Sales" }]);
  // jsdom lacks these blob-download primitives.
  (URL.createObjectURL as unknown) = vi.fn(() => "blob:x");
  (URL.revokeObjectURL as unknown) = vi.fn();
});

function renderPanel(onDone = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LookMLExportPanel projectId="p1" onDone={onDone} />
    </QueryClientProvider>,
  );
  return { onDone };
}

describe("LookMLExportPanel (F-020-04)", () => {
  it("shows the skipped-measure warning and does NOT auto-close when measures are skipped", async () => {
    exportModel.mockResolvedValue({ blob: new Blob(["x"]), warningCount: 3 });
    const { onDone } = renderPanel();
    const model = await screen.findByText("Sales");
    await userEvent.click(model);
    await waitFor(() =>
      expect(
        screen.getByText(/exportDialog.lookmlSkippedMeasures/),
      ).toBeInTheDocument(),
    );
    expect(onDone).not.toHaveBeenCalled();
  });

  it("auto-closes when nothing was skipped", async () => {
    exportModel.mockResolvedValue({ blob: new Blob(["x"]), warningCount: 0 });
    const { onDone } = renderPanel();
    const model = await screen.findByText("Sales");
    await userEvent.click(model);
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(
      screen.queryByText(/exportDialog.lookmlSkippedMeasures/),
    ).not.toBeInTheDocument();
  });
});
