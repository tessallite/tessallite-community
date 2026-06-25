import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProjectImportPanel from "./ProjectImportPanel";
import type {
  ProjectImportPlan,
  ProjectImportResponse,
} from "../../../api/importExportApi";

// --- Module mocks -----------------------------------------------------------

vi.mock("../../../api/importExportApi", () => ({
  projectImportExportApi: {
    importProject: vi.fn(),
  },
}));

vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "proj-1", slug: "acme", display_name: "Acme" }] }),
  useConnections: () => ({ data: [] }),
}));

vi.mock("../../Confirm", () => ({
  useConfirm: () => () => Promise.resolve(true),
}));

import { projectImportExportApi } from "../../../api/importExportApi";

const mockImport = projectImportExportApi.importProject as ReturnType<typeof vi.fn>;

// --- Fixtures ---------------------------------------------------------------

function bundle() {
  return {
    schema_version: 1,
    export_format: "tessallite-project/v1",
    exported_at: "2026-01-01T00:00:00Z",
    exported_from: {},
    credentials_included: false,
    credentials_envelope: null,
    included_sections: ["connections", "project_settings"],
    project: { slug: "acme", display_name: "Acme", is_active: true },
    connections: [],
    models: [{ model: { slug: "sales" } }, { model: { slug: "hr" } }],
  };
}

function replacePlan(): ProjectImportPlan {
  return {
    mode: "replace",
    target_project_id: "proj-1",
    target_project_slug: "acme",
    target_project_display_name: "Acme",
    target_project_exists: true,
    will_create_project: false,
    will_replace_project: true,
    delete_counts: { models: 2, project_settings: 5 },
    model_cascade_counts: {
      per_model: [
        {
          model_id: "m-1",
          slug: "sales",
          display_name: "Sales",
          counts: {
            aggregates: 3,
            pockets: 2,
            query_logs: 100,
            query_miss_logs: 10,
            route_logs: 50,
          },
        },
      ],
      totals: {
        aggregates: 3,
        pockets: 2,
        query_logs: 100,
        query_miss_logs: 10,
        route_logs: 50,
      },
    },
    incoming_counts: { models: 2, connections: 0, project_settings: 5 },
    connection_actions: [],
    model_slugs: ["sales", "hr"],
    post_import_actions: [],
    warnings: [],
  };
}

function planResponse(plan: ProjectImportPlan): ProjectImportResponse {
  return {
    project_id: null,
    project_slug: plan.target_project_slug,
    id_map: {},
    models_imported: 0,
    models_requiring_deploy: [],
    post_import_actions: plan.post_import_actions,
    warnings: plan.warnings,
    dry_run: true,
    plan,
  };
}

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ProjectImportPanel />
    </QueryClientProvider>,
  );
}

async function loadBundle() {
  const file = new File([JSON.stringify(bundle())], "acme.json", {
    type: "application/json",
  });
  const input = document.querySelector(
    'input[type="file"]',
  ) as HTMLInputElement;
  await userEvent.upload(input, file);
  await screen.findByText(/Bundle summary/i);
}

beforeEach(() => {
  mockImport.mockReset();
});

describe("ProjectImportPanel dry-run plan", () => {
  it("shows the plan with cascade volume before the import can be confirmed", async () => {
    mockImport.mockResolvedValueOnce(planResponse(replacePlan()));
    renderPanel();
    await loadBundle();

    // Import is blocked until the plan has been previewed.
    const importBtn = screen.getByRole("button", { name: "Import" });
    expect(importBtn).toBeDisabled();
    expect(
      screen.getByText(/Preview the plan to review/i),
    ).toBeInTheDocument();

    // Run the dry-run preview.
    await userEvent.click(
      screen.getByRole("button", { name: "Preview plan" }),
    );

    // The dry-run call must carry dry_run=true.
    await waitFor(() => expect(mockImport).toHaveBeenCalledTimes(1));
    expect(mockImport.mock.calls[0][0]).toMatchObject({ dry_run: true });

    // Plan is rendered before confirm: title + cascade blast radius.
    expect(await screen.findByText("Import plan")).toBeInTheDocument();
    expect(
      screen.getByText(/Cascade volume deleted/i),
    ).toBeInTheDocument();
    // Cascade totals (aggregates/pockets/logs) are surfaced.
    expect(
      screen.getByText(/Totals: 3 aggregates, 2 pockets, 100 query logs/i),
    ).toBeInTheDocument();
    // Per-model cascade line (logs = 100 + 10 + 50 = 160).
    expect(screen.getByText(/Sales:.*160 log rows/i)).toBeInTheDocument();

    // Now the import button is enabled.
    expect(
      screen.getByRole("button", { name: "Import" }),
    ).not.toBeDisabled();
  });

  it("invalidates the plan when the import mode changes, forcing a re-preview", async () => {
    mockImport.mockResolvedValueOnce(planResponse(replacePlan()));
    renderPanel();
    await loadBundle();

    await userEvent.click(
      screen.getByRole("button", { name: "Preview plan" }),
    );
    expect(await screen.findByText("Import plan")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Import" }),
    ).not.toBeDisabled();

    // Switch to replace -> plan invalidated, import disabled again.
    await userEvent.click(screen.getByRole("radio", { name: /Replace/i }));
    await waitFor(() =>
      expect(screen.queryByText("Import plan")).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
  });
});
