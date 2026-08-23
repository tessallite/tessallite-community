import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const postMock = vi.fn();
const navigateMock = vi.fn();
const projectsListMock = vi.fn();
const modelsListMock = vi.fn();

vi.mock("../api/client", () => ({
  default: { post: (...args: unknown[]) => postMock(...args) },
  projectsApi: {
    list: (...args: unknown[]) => projectsListMock(...args),
    delete: vi.fn(),
  },
  modelsApi: {
    list: (...args: unknown[]) => modelsListMock(...args),
    delete: vi.fn(),
  },
  accessApi: { list: vi.fn().mockResolvedValue([]), revoke: vi.fn() },
  authApi: { listTenantUsers: vi.fn().mockResolvedValue([]) },
  webhooksApi: { dlqCount: vi.fn().mockResolvedValue({ count: 0 }) },
}));

vi.mock("../api/agentApi", () => ({
  agentApi: { getConfig: vi.fn().mockResolvedValue({}) },
}));

vi.mock("../components/Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

vi.mock("../components/HelpIconButton", () => ({ default: () => null }));
vi.mock("../components/Settings/ProjectConfigDrawer", () => ({ default: () => null }));
vi.mock("../components/Settings/EffectiveAccessPreview", () => ({ default: () => null }));
vi.mock("../components/Admin/GitSettingsPanel", () => ({ default: () => null }));
vi.mock("../components/Admin/SsoSettingsPanel", () => ({ default: () => null }));
vi.mock("../components/Admin/EmbedTokensPanel", () => ({ default: () => null }));
vi.mock("../components/Admin/SecurityAuditPanel", () => ({ default: () => null }));
vi.mock("../components/Admin/grantAccessWithSupersede", () => ({
  grantAccessWithSupersede: vi.fn(),
}));

vi.mock("../auth/passwordPolicy", () => ({
  meetsPasswordPolicy: () => true,
  showsPasswordPolicyError: () => false,
}));

vi.mock("../utils/extractApiError", () => ({
  extractApiError: () => "request failed",
}));

vi.mock("../i18n", () => ({
  useT: () => (key: string, vars?: Record<string, string | number>) => {
    const messages: Record<string, string> = {
      "tenantAdmin.deployTooltip": "Deploy model",
      "tenantAdmin.undeployTooltip": "Undeploy model",
      "tenantAdmin.statusDraft": "Draft",
      "tenantAdmin.statusDeployed": "Deployed",
      "deploy.joinPopulationBlockedTitle": "Deployment blocked by join-population policy",
      "deploy.joinPopulationBlockedSummary": "A measured join effect is above the system threshold ({{threshold}}).",
      "deploy.joinPopulationOffenders": "Join-population offenders",
      "deploy.joinPopulationUnknownJoin": "Join {{id}}",
      "deploy.joinPopulationOffenderDetail": "Measured effect: {{effect}} · reason: {{reason}}",
      "deploy.joinPopulationMeasuredReason": "measured",
      "deploy.joinPopulationBlockedAction": "Declare the join's population role accurately or fix the join/source data, then deploy again.",
      "deploy.openJoins": "Open Joins",
    };
    let text = messages[key] ?? key;
    for (const [name, value] of Object.entries(vars ?? {})) {
      text = text.split(`{{${name}}}`).join(String(value));
    }
    return text;
  },
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigateMock };
});

import TenantAdmin from "./TenantAdmin";

const project = {
  id: "project-1",
  slug: "analytics",
  display_name: "Analytics",
  is_active: true,
};
const model = {
  id: "model-1",
  slug: "sales",
  display_name: "Sales",
  deployed_version_id: null,
  last_deployed_at: null,
};

function renderAdmin() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TenantAdmin />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.setItem("tenant_id", "tenant-1");
  projectsListMock.mockResolvedValue([project]);
  modelsListMock.mockResolvedValue([model]);
  postMock.mockRejectedValue({
    response: {
      data: {
        detail: {
          code: "JOIN_POPULATION_BLOCKED",
          message: "deployment refused",
          threshold: 0.15,
          joins: [
            {
              join_id: "join-1",
              join_label: "Fact.customer_id ↔ Customer.id",
              population_participation: "undeclared",
              status: "BLOCKED",
              row_effect_ratio: 0.2,
              reason: "measured row effect exceeds threshold",
            },
            {
              join_id: "join-2",
              join_label: "Fact.region_id ↔ Region.id",
              population_participation: "enrichment_only",
              status: "BLOCKED",
              row_effect_ratio: 0.18,
              reason: "filtering enrichment effect exceeds threshold",
            },
          ],
        },
      },
    },
  });
});

describe("TenantAdmin join-population deploy refusal", () => {
  it("is explicit, actionable, and does not turn the model into a deployed state", async () => {
    renderAdmin();
    await userEvent.click(await screen.findByText("Analytics"));
    await screen.findByText("Sales");

    await userEvent.click(screen.getByRole("button", { name: "Deploy model" }));
    await waitFor(() => expect(postMock).toHaveBeenCalledWith(
      "/api/v1/projects/project-1/models/model-1/deploy",
    ));
    await waitFor(() =>
      expect(screen.getByTestId("join-population-blocked-notice")).toBeInTheDocument(),
    );
    expect(screen.getByText("Fact.customer_id ↔ Customer.id")).toBeInTheDocument();
    expect(screen.getByText("Fact.region_id ↔ Region.id")).toBeInTheDocument();
    expect(screen.getByText("Draft")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Open Joins" }));
    expect(navigateMock).toHaveBeenCalledWith(
      "/tenants/tenant-1/projects/project-1/models/model-1?panel=joins",
    );
  });
});
