/**
 * Bug-9316 / G-021-02 — identity-provider overlay fields render on the live
 * Workspace Settings surface (ProjectConfigDrawer), not a missing /admin tab.
 *
 * Missing SsoSettingsPanel.tsx must fail this suite at import time.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const getConfigMock = vi.fn();
const putConfigMock = vi.fn();
const getBackendsMock = vi.fn();

vi.mock("../../api/client", () => ({
  ssoApi: {
    getConfig: (...a: unknown[]) => getConfigMock(...a),
    putConfig: (...a: unknown[]) => putConfigMock(...a),
    getBackends: (...a: unknown[]) => getBackendsMock(...a),
  },
  embedTokensApi: {
    list: vi.fn().mockResolvedValue([]),
    revoke: vi.fn(),
  },
}));

vi.mock("../../i18n", () => ({ useT: () => (key: string) => key }));
vi.mock("../../auth/currentUser", () => ({ isTenantAdmin: () => true }));
vi.mock("../HelpIconButton", () => ({ default: () => null }));
vi.mock("../Panels/ConnectionsPanel", () => ({
  default: () => <div>connections-stub</div>,
}));
vi.mock("./ProjectLLMScreen", () => ({ default: () => null }));
vi.mock("./BrandingPanel", () => ({ default: () => null }));
vi.mock("./DemoTenantPanel", () => ({ default: () => null }));
vi.mock("./ProjectAgentTabs", () => ({ default: () => null }));
vi.mock("../Admin/UsersAccessPanel", () => ({ default: () => null }));
vi.mock("../Admin/SecurityAuditPanel", () => ({ default: () => null }));
vi.mock("../../pages/AuditLog", () => ({ default: () => null }));
vi.mock("../../pages/Webhooks", () => ({ default: () => null }));
vi.mock("../../pages/GroupMappings", () => ({
  default: () => <div>sso-mappings-stub</div>,
}));

import ProjectConfigDrawer from "./ProjectConfigDrawer";
import SsoSettingsPanel from "../Admin/SsoSettingsPanel";

const project = {
  id: "p1",
  slug: "demo",
  display_name: "Demo project",
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
};

function renderDrawer() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ProjectConfigDrawer project={project} open onClose={() => undefined} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getConfigMock.mockResolvedValue({
    oidc: { issuer: "", client_id: "", client_secret_set: false },
    saml: { idp_metadata_url: "" },
  });
  getBackendsMock.mockResolvedValue({
    backends: ["local"],
    saml_enabled: false,
    oidc_enabled: false,
    ldap_enabled: false,
    gcp_iam_enabled: false,
  });
});

describe("Bug-9316 / G-021-02 — live drawer Identity provider overlay", () => {
  it("imports SsoSettingsPanel (missing module fails the suite)", () => {
    expect(SsoSettingsPanel).toBeTypeOf("function");
  });

  it("renders OIDC issuer and SAML metadata fields from the live drawer section", async () => {
    renderDrawer();
    await userEvent.click(screen.getByText("projectNav.ssoIdp"));
    await waitFor(() => {
      expect(screen.getByLabelText("sso.oidcIssuer")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("sso.samlMetadataUrl")).toBeInTheDocument();
    expect(screen.getByLabelText("sso.oidcClientId")).toBeInTheDocument();
    expect(screen.getByLabelText("sso.oidcClientSecret")).toBeInTheDocument();
  });
});
