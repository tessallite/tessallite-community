import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import enAdmin from "../i18n/en/admin.json";
import arAdmin from "../i18n/ar/admin.json";
import deAdmin from "../i18n/de/admin.json";
import esAdmin from "../i18n/es/admin.json";
import frAdmin from "../i18n/fr/admin.json";
import jaAdmin from "../i18n/ja/admin.json";
import ptAdmin from "../i18n/pt/admin.json";
import zhAdmin from "../i18n/zh/admin.json";

// The SSO backends probe is irrelevant to the about line; keep its promise
// from ever resolving so it cannot fire a setState after the test ends.
vi.mock("../api/client", () => ({
  ssoApi: { getBackends: vi.fn(() => new Promise(() => {})) },
  authApi: {},
}));

const BUILD_VARS = [
  "VITE_TESSALLITE_VERSION",
  "VITE_DEPLOYMENT_TYPE",
  "VITE_BUILD_COMMIT_HASH",
] as const;

function clearBuildVars() {
  for (const key of BUILD_VARS) delete process.env[key];
}

function setBuildVars(vars: Partial<Record<(typeof BUILD_VARS)[number], string>>) {
  clearBuildVars();
  Object.assign(process.env, vars);
}

// Login.tsx reads the build variables into module-level constants at import
// time (Vite bakes them in at build), so each scenario re-imports the module
// after setting the env — the same pattern as EndpointsPanel.test.tsx.
async function renderLogin() {
  vi.resetModules();
  const { default: Login } = await import("./Login");
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Login />
    </MemoryRouter>,
  );
}

beforeEach(clearBuildVars);
afterEach(clearBuildVars);

describe("Login about line (Bug-9558)", () => {
  it.each([
    "Dev Stack",
    "Community Edition",
    "Enterprise Edition",
    "Cloud Edition",
  ])("renders the exact about line for %s", async (type) => {
    setBuildVars({
      VITE_TESSALLITE_VERSION: "1.1.6",
      VITE_DEPLOYMENT_TYPE: type,
      // Longer than 13 chars on purpose: the line must show the first 13.
      VITE_BUILD_COMMIT_HASH: "abcdef1234567890",
    });
    await renderLogin();
    expect(
      screen.getByText(`Tessallite ${type} v1.1.6 [abcdef1234567]`),
    ).toBeInTheDocument();
  });

  // Bug-9558 DR-02: this test used to assert "Tessallite vunknown" (a
  // malformed string — the template's literal " v" prefix had no space
  // before the substituted "unknown" fallback) as the CORRECT degraded
  // output. That was the actual production behavior of every real deploy,
  // since no Dockerfile/compose/deploy script supplied the three build vars.
  // The fix omits the version portion entirely when unset, exactly like the
  // deployment-type and commit-hash portions already do.
  it("falls back gracefully when no build variable is set", async () => {
    await renderLogin();
    const about = screen.getByTestId("login-about");
    expect(about.textContent).toBe("Tessallite");
    expect(about.textContent).not.toMatch(/undefined|null|unknown/i);
    expect(about.textContent).not.toContain("[");
  });

  it("omits the hash portion when only the hash variable is unset", async () => {
    setBuildVars({
      VITE_TESSALLITE_VERSION: "2.0.0",
      VITE_DEPLOYMENT_TYPE: "Enterprise Edition",
    });
    await renderLogin();
    expect(
      screen.getByText("Tessallite Enterprise Edition v2.0.0"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("login-about").textContent).not.toContain("[");
  });

  it("omits the deployment type portion when only it is unset", async () => {
    setBuildVars({
      VITE_TESSALLITE_VERSION: "2.0.0",
      VITE_BUILD_COMMIT_HASH: "abcdef1234567890",
    });
    await renderLogin();
    expect(
      screen.getByText("Tessallite v2.0.0 [abcdef1234567]"),
    ).toBeInTheDocument();
  });

  // Bug-9558 R2-07: the frontend image is one Community/Enterprise
  // runtime license-flip, so the build-time VITE_DEPLOYMENT_TYPE label can be
  // stale (e.g. a Community bundle with an Enterprise license installed).
  // Per user decision 2026-08-24, install.sh writes a static /edition.json
  // artifact; Login.tsx fetches it once and overrides the label when present.
  describe("edition.json override (R2-07)", () => {
    const originalFetch = global.fetch;

    afterEach(() => {
      global.fetch = originalFetch;
    });

    it("overrides the build-time deployment type with the installed edition", async () => {
      setBuildVars({
        VITE_TESSALLITE_VERSION: "1.1.6",
        VITE_DEPLOYMENT_TYPE: "Community Edition",
        VITE_BUILD_COMMIT_HASH: "abcdef1234567890",
      });
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ edition: "enterprise", activated: true }),
      }) as unknown as typeof fetch;

      await renderLogin();

      await waitFor(() =>
        expect(
          screen.getByText("Tessallite Enterprise Edition v1.1.6 [abcdef1234567]"),
        ).toBeInTheDocument(),
      );
    });

    it("keeps the build-time label when edition.json is absent (404)", async () => {
      setBuildVars({
        VITE_TESSALLITE_VERSION: "1.1.6",
        VITE_DEPLOYMENT_TYPE: "Community Edition",
      });
      global.fetch = vi.fn().mockResolvedValue({ ok: false }) as unknown as typeof fetch;

      await renderLogin();

      await waitFor(() =>
        expect(
          screen.getByText("Tessallite Community Edition v1.1.6"),
        ).toBeInTheDocument(),
      );
    });

    it("keeps the build-time label when the fetch itself fails", async () => {
      setBuildVars({
        VITE_TESSALLITE_VERSION: "1.1.6",
        VITE_DEPLOYMENT_TYPE: "Community Edition",
      });
      global.fetch = vi.fn().mockRejectedValue(new Error("network error")) as unknown as typeof fetch;

      await renderLogin();

      await waitFor(() =>
        expect(
          screen.getByText("Tessallite Community Edition v1.1.6"),
        ).toBeInTheDocument(),
      );
    });

    // Bug-9578 (R3-06, round-2 recheck): formatEditionLabel used to blindly
    // title-case whatever string /api/v1/edition returned, so an internal
    // license state (e.g. "internal-unlimited", "unactivated") would leak to
    // the login page as "Internal-unlimited Edition". The allow-list must
    // recognise the finite known domain (community, enterprise,
    // internal-unlimited) and keep the build-time label for anything else.
    it("keeps the build-time label for an unrecognised edition string instead of title-casing it", async () => {
      setBuildVars({
        VITE_TESSALLITE_VERSION: "1.1.6",
        VITE_DEPLOYMENT_TYPE: "Community Edition",
      });
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ edition: "unactivated", activated: false }),
      }) as unknown as typeof fetch;

      await renderLogin();

      await waitFor(() =>
        expect(
          screen.getByText("Tessallite Community Edition v1.1.6"),
        ).toBeInTheDocument(),
      );
      expect(screen.queryByText(/Unactivated/)).not.toBeInTheDocument();
    });

    it("recognises the internal-unlimited edition without title-casing it raw", async () => {
      setBuildVars({
        VITE_TESSALLITE_VERSION: "1.1.6",
        VITE_DEPLOYMENT_TYPE: "Dev Stack",
      });
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ edition: "internal-unlimited", activated: false }),
      }) as unknown as typeof fetch;

      await renderLogin();

      await waitFor(() =>
        expect(
          screen.getByText("Tessallite Internal Edition v1.1.6"),
        ).toBeInTheDocument(),
      );
    });
  });

  it("defines login.about in en and every other locale", () => {
    const bundles = [
      enAdmin,
      arAdmin,
      deAdmin,
      esAdmin,
      frAdmin,
      jaAdmin,
      ptAdmin,
      zhAdmin,
    ];
    for (const bundle of bundles) {
      expect(bundle["login.about"]).toBe(
        "Tessallite{{deploymentType}}{{version}}{{commitHash}}",
      );
    }
  });
});
