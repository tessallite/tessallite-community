import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Bug-9308: the GCP IAM status line already stated env-only configuration
// (sso.gcpIamOn/Off) but offered no help link — the only missing piece per
// the honest-label disposition. No audience/domain fields are added (that
// needs a backend config API, out of scope).

const getConfigMock = vi.fn();
const getBackendsMock = vi.fn();

vi.mock("../../api/client", () => ({
  ssoApi: {
    getConfig: () => getConfigMock(),
    getBackends: () => getBackendsMock(),
    putConfig: vi.fn(),
  },
}));

import SsoSettingsPanel from "./SsoSettingsPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SsoSettingsPanel />
    </QueryClientProvider>,
  );
}

describe("SsoSettingsPanel GCP IAM honest-label help link (Bug-9308)", () => {
  beforeEach(() => {
    getConfigMock.mockReset().mockResolvedValue({ issuer: "", client_id: "", client_secret_set: false, metadata_url: "" });
    getBackendsMock.mockReset();
  });

  it("shows a help link next to the GCP IAM status pointing at the SSO configuration help page", async () => {
    getBackendsMock.mockResolvedValue({ gcp_iam_enabled: false, ldap_enabled: false });
    renderPanel();

    const link = await screen.findByRole("link", { name: /help/i });
    expect(link).toHaveAttribute("href", "/help/admin/sso-configuration.html");
  });

  it("still shows the honest env-only status text regardless of on/off state", async () => {
    getBackendsMock.mockResolvedValue({ gcp_iam_enabled: true, ldap_enabled: false });
    renderPanel();

    expect(await screen.findByText(/set on the server, not in this form/i)).toBeTruthy();
  });
});
