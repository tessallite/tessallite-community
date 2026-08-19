import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DbtImportPanel from "./DbtImportPanel";
import { dbtImportApi } from "../../../api/importExportApi";

// F-020-03 / G-020-03: the SPA must PREVIEW the loss/warning report (dry-run)
// before the models are created, so a migration engineer sees what will not
// transfer. Import is blocked until a preview of the current file exists.
vi.mock("../../../api/importExportApi", () => ({
  dbtImportApi: { importDbt: vi.fn() },
}));

const importDbt = dbtImportApi.importDbt as ReturnType<typeof vi.fn>;

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DbtImportPanel projectId="p1" />
    </QueryClientProvider>,
  );
}

async function chooseFile() {
  const file = new File(["metrics: []"], "dbt.yml", { type: "text/yaml" });
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.upload(input, file);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("DbtImportPanel preview-before-apply (F-020-03)", () => {
  it("blocks Import until a preview is run, and previews with dry_run", async () => {
    importDbt.mockResolvedValue({
      models_parsed: 2,
      models_created: 0,
      model_names: ["orders", "customers"],
      warnings: [
        { kind: "measure_disabled", detail: "cumulative metric skipped" },
      ],
    });
    renderPanel();
    await chooseFile();

    const importBtn = screen.getByRole("button", { name: /^Import$/i });
    expect(importBtn).toBeDisabled(); // no preview yet

    await userEvent.click(screen.getByRole("button", { name: /Preview/i }));
    // Preview call carries dry_run=true.
    await waitFor(() => expect(importDbt).toHaveBeenCalledTimes(1));
    expect(importDbt.mock.calls[0][2]).toBe(true);

    // The warning report is shown before apply.
    expect(
      await screen.findByTestId("import-warning-alerts"),
    ).toBeInTheDocument();
    // Import is now enabled.
    expect(screen.getByRole("button", { name: /^Import$/i })).not.toBeDisabled();
  });

  it("applies with dry_run=false after a preview", async () => {
    importDbt.mockResolvedValue({
      models_parsed: 1,
      models_created: 0,
      model_names: ["orders"],
      warnings: [],
    });
    renderPanel();
    await chooseFile();
    await userEvent.click(screen.getByRole("button", { name: /Preview/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Import$/i })).not.toBeDisabled(),
    );

    importDbt.mockResolvedValueOnce({
      models_parsed: 1,
      models_created: 1,
      model_names: ["orders"],
      warnings: [],
    });
    await userEvent.click(screen.getByRole("button", { name: /^Import$/i }));
    await waitFor(() => expect(importDbt).toHaveBeenCalledTimes(2));
    // The apply call omits the dry-run flag (default false).
    expect(importDbt.mock.calls[1][2]).toBeUndefined();
  });
});
