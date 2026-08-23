import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { I18nContext } from "../i18n";
import en from "../i18n";

vi.mock("../api/hooks", () => {
  const query = { data: [], isLoading: false, isSuccess: true };
  return {
    useAggregates: () => query,
    useAllModelTables: () => query,
    useDimensions: () => query,
    useHierarchies: () => query,
    useJoins: () => query,
    useMeasures: () => query,
    useModel: () => ({
      data: {
        id: "model-1",
        display_name: "Model",
        status: "active",
        deployed_version_id: null,
        deployed_version_number: null,
        last_saved_version_number: 1,
        last_deployed_at: null,
        canvas_layout: {},
      },
      isLoading: false,
    }),
    usePockets: () => query,
    useProject: () => ({ data: { id: "project-1", display_name: "Project", slug: "project" }, isLoading: false }),
    useSources: () => query,
    useTargets: () => query,
  };
});

vi.mock("../api/client", () => ({
  modelsApi: { update: vi.fn().mockResolvedValue({}) },
}));

vi.mock("../api/versionsApi", () => ({
  versionsApi: {
    create: vi.fn().mockResolvedValue({ version_number: 1 }),
    deploy: vi.fn().mockResolvedValue({ last_deployed_at: null }),
    undeploy: vi.fn().mockResolvedValue({}),
  },
}));

vi.mock("../components/Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

vi.mock("../components/Builder/Canvas", () => ({
  default: () => <div data-testid="canvas" />,
}));
vi.mock("../components/Builder/Drawer", () => ({
  default: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock("../components/Builder/MiniTabs", () => ({
  default: () => <div data-testid="mini-tabs" />,
}));
vi.mock("../components/Builder/StatusBar", () => ({
  default: () => <div data-testid="status-bar" />,
}));
vi.mock("../components/Builder/Toolbelt", () => ({
  default: () => <div data-testid="toolbelt" />,
}));
vi.mock("../components/Builder/ValidationTray", () => ({
  default: () => <div data-testid="validation-tray" />,
}));
vi.mock("../components/Builder/UnsavedChangesGuard", () => ({
  default: () => null,
}));
vi.mock("../components/Builder/ShortcutHelpDialog", () => ({
  default: () => null,
}));
vi.mock("../components/Builder/useModelValidation", () => ({
  useModelValidation: vi.fn(),
}));
vi.mock("../hooks/useGlobalShortcuts", () => ({
  useGlobalShortcuts: vi.fn(),
}));
vi.mock("../components/HelpIconButton", () => ({
  default: () => null,
}));

import ModelBuilder from "./ModelBuilder";

function renderBuilder(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={qc}>
        <I18nContext.Provider value={en}>
          <Routes>
            <Route
              path="/tenants/:tenantId/projects/:projectId/models/:modelId"
              element={<ModelBuilder />}
            />
          </Routes>
        </I18nContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("ModelBuilder read-only page controls (Bug-5966)", () => {
  it("disables the model-enabled switch under ?readonly=1", () => {
    renderBuilder("/tenants/acme/projects/project-1/models/model-1?readonly=1");

    expect(screen.getByRole("checkbox", { name: /model enabled/i })).toBeDisabled();
  });
});
