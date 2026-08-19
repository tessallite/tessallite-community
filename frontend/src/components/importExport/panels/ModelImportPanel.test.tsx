import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ModelImportPanel from "./ModelImportPanel";
import { connectionsApi } from "../../../api/client";
import { importExportApi } from "../../../api/importExportApi";

vi.mock("../../../api/client", () => ({
  connectionsApi: {
    list: vi.fn(),
  },
}));

vi.mock("../../../api/importExportApi", () => ({
  importExportApi: {
    importModel: vi.fn(),
  },
}));

vi.mock("../../../i18n", () => ({
  useT: () => (key: string, vars?: Record<string, unknown>) =>
    vars?.format ? `${key}:${String(vars.format)}` : key,
}));

const listConnections = connectionsApi.list as ReturnType<typeof vi.fn>;
const importModel = importExportApi.importModel as ReturnType<typeof vi.fn>;

function legacyBundle() {
  return {
    schema_version: 1,
    export_format: "tessallite-model/v1",
    exported_at: "2026-01-01T00:00:00Z",
    exported_from: {},
    model_display_name: "Sales Model",
    model_slug: "sales-model",
    snapshot: {
      data_sources: [
        {
          project_connection_id: "source-1",
          display_name: "Legacy Source",
          source_type: "import_placeholder",
        },
      ],
      data_targets: [],
    },
  };
}

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ModelImportPanel projectId="project-1" />
    </QueryClientProvider>,
  );
}

async function loadLegacyBundle() {
  const file = new File([JSON.stringify(legacyBundle())], "model.json", {
    type: "application/json",
  });
  const input = document.querySelector(
    'input[type="file"]',
  ) as HTMLInputElement;
  await userEvent.upload(input, file);
  await screen.findByText(/Legacy Source \(import_placeholder\)/);
}

beforeEach(() => {
  vi.clearAllMocks();
  listConnections.mockResolvedValue([
    {
      id: "conn-pg",
      display_name: "Postgres local",
      connection_type: "postgresql",
    },
    {
      id: "conn-bq",
      display_name: "BigQuery local",
      connection_type: "bigquery",
    },
  ]);
  importModel.mockResolvedValue({
    model_id: "model-1",
    slug: "sales-model",
    display_name: "Sales Model",
    deployed_version_id: null,
    missing_connections: [],
  });
});

describe("ModelImportPanel connection mapping", () => {
  it("does not dead-end legacy importer stubs whose source_type has no local connection_type match", async () => {
    renderPanel();
    const user = userEvent.setup();

    await loadLegacyBundle();

    await user.click(await screen.findByRole("combobox"));
    const listbox = await screen.findByRole("listbox");
    expect(within(listbox).getByText("Postgres local")).toBeInTheDocument();
    expect(within(listbox).getByText("BigQuery local")).toBeInTheDocument();

    await user.click(within(listbox).getByText("Postgres local"));
    await user.click(screen.getByRole("button", { name: "importDialog.importButton" }));

    await waitFor(() => expect(importModel).toHaveBeenCalledTimes(1));
    expect(importModel.mock.calls[0][0]).toBe("project-1");
    expect(importModel.mock.calls[0][1]).toMatchObject({
      target_project_id: "project-1",
      target_slug: "sales-model",
      target_display_name: "Sales Model",
      connection_mapping: { "source-1": "conn-pg" },
    });
    expect(importModel.mock.calls[0][1].bundle).not.toHaveProperty(
      "connections_required",
    );
  });
});
