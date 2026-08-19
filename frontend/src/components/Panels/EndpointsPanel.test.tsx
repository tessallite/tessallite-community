import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// useModel/useProject hit the API; stub them with deployed slugs.
vi.mock("../../api/hooks", () => ({
  useModel: () => ({ data: { slug: "modelx" }, isLoading: false }),
  useProject: () => ({ data: { slug: "project1" }, isLoading: false }),
}));

// EndpointsPanel is a large MUI tree that is re-imported (vi.resetModules) and
// re-rendered for each host scenario; a cold render can exceed the 10s default.
vi.setConfig({ testTimeout: 30000 });

const WARNING_RE = /gateway address for this deployment could not be verified/i;

const originalLocation = window.location;

function setHost(hostname: string, protocol = "https:") {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      hostname,
      protocol,
      origin: `${protocol}//${hostname}`,
    },
  });
}

// EndpointsPanel resolves the gateway address at module-load time (Vite bakes
// the build-args in then). Re-import the module per test after configuring the
// host so the module-level constants reflect the scenario under test.
async function renderPanel() {
  vi.resetModules();
  const mod = await import("./EndpointsPanel");
  const EndpointsPanel = mod.default;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        initialEntries={["/p/project-1/m/model-1"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/p/:projectId/m/:modelId" element={<EndpointsPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
});

describe("EndpointsPanel gateway address resolution (Bug-5540)", () => {
  // The first renderPanel() in the file pays the whole EndpointsPanel module
  // graph import (MUI et al.); under full-suite parallel load this exceeds the
  // 30 s default. Assertions are unchanged — only the budget is raised.
  it("warns when on a real deployment with no gateway build-arg or override", async () => {
    // No VITE_GATEWAY_URL build-arg, no settings override, non-local host:
    // the host-based fallback would point at the app domain — must warn.
    setHost("cloud.tessallite.io");
    await renderPanel();
    // Warning renders in both the JDBC and XMLA endpoint panels.
    expect(screen.getAllByText(WARNING_RE).length).toBeGreaterThan(0);
  }, 120_000);

  it("does not warn and uses the configured host when a settings override exists", async () => {
    // Admin pinned the gateway address via Settings — reliable, no warning.
    setHost("cloud.tessallite.io");
    localStorage.setItem(
      "builder.settings.gatewayHttpUrl",
      "https://sql.cloud.tessallite.io:8080",
    );
    localStorage.setItem(
      "builder.settings.gatewayJdbcHost",
      "sql.cloud.tessallite.io",
    );
    await renderPanel();
    expect(screen.queryByText(WARNING_RE)).not.toBeInTheDocument();
    expect(
      screen.getAllByText("Host: sql.cloud.tessallite.io").length,
    ).toBeGreaterThan(0);
  });

  it("Power BI section shows PostgreSQL connector (port 5433) with tenant slug as database", async () => {
    setHost("cloud.tessallite.io");
    localStorage.setItem("tenant_id", "acme-demo");
    localStorage.setItem(
      "builder.settings.gatewayHttpUrl",
      "https://sql.cloud.tessallite.io:8080",
    );
    localStorage.setItem(
      "builder.settings.gatewayJdbcHost",
      "sql.cloud.tessallite.io",
    );

    await renderPanel();

    // Power BI tab (default tab=0 in XMLA accordion) shows PostgreSQL recipe
    // with the JDBC host, port 5433, and tenant slug as the database name.
    // The instructions are rendered inside a pre block.
    expect(
      screen.getByText(/Server: sql\.cloud\.tessallite\.io:5433/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/PostgreSQL database/),
    ).toBeInTheDocument();

    // The Power BI chip shows "Database: acme-demo" (tenant slug only).
    const dbChips = screen.getAllByText(/Database: acme-demo/);
    const hasTenantOnlyChip = dbChips.some(
      (el) => el.textContent === "Database: acme-demo",
    );
    expect(hasTenantOnlyChip).toBe(true);

    // Power BI section must NOT contain XMLA URL or MSOLAP connection string.
    expect(
      screen.queryByText(/Provider=MSOLAP/),
    ).not.toBeInTheDocument();

    // Bug-7314: SSO users must be told to use a Personal Access Token as the
    // password. The note appears in the Power BI section.
    expect(
      screen.getAllByText(/Personal Access Token/i).length,
    ).toBeGreaterThan(0);
  });

  it("Excel section still shows tenantless XMLA server URL", async () => {
    setHost("cloud.tessallite.io");
    localStorage.setItem("tenant_id", "acme-demo");
    localStorage.setItem(
      "builder.settings.gatewayHttpUrl",
      "https://sql.cloud.tessallite.io:8080",
    );
    localStorage.setItem(
      "builder.settings.gatewayJdbcHost",
      "sql.cloud.tessallite.io",
    );

    await renderPanel();

    // Switch to the Excel tab inside the XMLA accordion to render
    // the Excel instructions. MUI Tab buttons have role="tab".
    const excelTabs = screen.getAllByText(/^Excel$/);
    // Click the last one — the XMLA accordion's Excel tab.
    fireEvent.click(excelTabs[excelTabs.length - 1]);

    // Excel instructions should contain the tenantless XMLA server URL.
    const excelUrl = "https://sql.cloud.tessallite.io:8080/api/v1/xmla/";
    expect(
      screen.getByText(new RegExp(excelUrl.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))),
    ).toBeInTheDocument();
  });

  it("does not warn on local dev where the host-based fallback is correct", async () => {
    setHost("localhost", "http:");
    await renderPanel();
    expect(screen.queryByText(WARNING_RE)).not.toBeInTheDocument();
    expect(screen.getAllByText("Host: localhost").length).toBeGreaterThan(0);
  });
});

describe("EndpointsPanel Excel manifest generation", () => {
  it("generates a custom-functions-capable TESSALLITE manifest", async () => {
    vi.resetModules();
    const { generateManifest } = await import("./EndpointsPanel");
    const manifest = generateManifest("https://cloud.tessallite.io/");

    // Bug-6905: V1_0 is required for custom functions on perpetual Office;
    // the Script URL must be the IIFE bundle, registration runs via the Page URL.
    expect(manifest).toContain('xsi:type="VersionOverridesV1_0"');
    expect(manifest).not.toContain('xsi:type="VersionOverridesV1_1"');
    expect(manifest).toContain('xsi:type="CustomFunctions"');
    expect(manifest).toContain("Functions.Script.Url");
    expect(manifest).toContain("Functions.Page.Url");
    expect(manifest).toContain("Functions.Metadata.Url");
    expect(manifest).toContain("Functions.Namespace");
    expect(manifest).toContain('DefaultValue="TESSALLITE"');
    expect(manifest).toContain(
      'DefaultValue="https://cloud.tessallite.io/excel-plugin/functions.iife.js"',
    );
    expect(manifest).toContain(
      'DefaultValue="https://cloud.tessallite.io/excel-plugin/functions.html"',
    );
    expect(manifest).toContain(
      'DefaultValue="https://cloud.tessallite.io/excel-plugin/functions.json"',
    );
    expect(manifest).toContain('Name="CustomFunctionsRuntime"');
    expect(manifest).toContain("<Version>1.0.0.9</Version>");
  });
});
