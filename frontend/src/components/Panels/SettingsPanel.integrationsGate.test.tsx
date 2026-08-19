/**
 * Bug-8170 — the Collibra / Solidatus settings tabs exposed admin/modeler-only
 * mutations (create, activate/deactivate, edit, delete, sync) with no
 * client-side gate: a viewer could open either tab and be offered controls
 * the backend (api/collibra.py, api/solidatus.py — require_role("admin") for
 * create/update/delete, require_role("modeler") for validate/sync) would
 * reject. This proves the tabs are gated on the server-derived per-model
 * authoring authority. It is a UI gate only — the backend role checks remain
 * the actual enforcement.
 *
 * Round-2 fix (review-blocked, root cause): the first pass gated on the
 * coarse local role via `canEditModelConfig()`. That check is wrong for the
 * canonical locally-provisioned modeller — local role `member` plus a
 * per-project `modeler` binding — because `modeler` is never a
 * `LocalUser.role` value; it is a per-project binding the backend resolves
 * into `caller_can_author` (Bug-8747 /
 * questions_modeller-authoring-authority-source.md, decided: fail closed on
 * that per-model signal, no coarse-role fallback). A coarse-role gate wrongly
 * hid both tabs from that authorized modeller. The fix — and this test —
 * gate on `useCanAuthorModel()` (backed by the builder-store `readOnly` flag
 * that `caller_can_author` drives), the same source every other Model
 * Builder authoring panel already uses.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const authState = vi.hoisted(() => ({ canAuthor: true }));

vi.mock("../../auth/useCanAuthorModel", () => ({
  useCanAuthorModel: () => authState.canAuthor,
}));
vi.mock("../Settings/ModelConfigurationPanel", () => ({ default: () => null }));
vi.mock("../Settings/ModelLLMFunctions", () => ({ default: () => null }));
vi.mock("../Settings/SLAConfigPanel", () => ({ SLAConfigPanel: () => null }));
vi.mock("./SolidatusIntegrationPanel", () => ({
  SolidatusIntegrationPanel: () => <div data-testid="solidatus-panel" />,
}));
vi.mock("./CollibraIntegrationPanel", () => ({
  CollibraIntegrationPanel: () => <div data-testid="collibra-panel" />,
}));
vi.mock("../../i18n", () => ({ useT: () => (key: string) => key }));

import SettingsPanel from "./SettingsPanel";

function renderPanel() {
  return render(<SettingsPanel projectId="p-1" modelId="m-1" />);
}

beforeEach(() => {
  localStorage.clear();
  authState.canAuthor = true;
});

describe("SettingsPanel — Collibra/Solidatus tab gate (Bug-8170)", () => {
  it("shows both integration tabs for a local member with a project modeler binding (readOnly=false)", () => {
    // The exact case the coarse local-role check got wrong: local role is
    // `member` (never "modeler" — that is a per-project binding), but the
    // backend resolved caller_can_author=true for this project, which
    // ModelBuilder.tsx turns into builder-store readOnly=false.
    localStorage.setItem("user_role", "member");
    authState.canAuthor = true;
    renderPanel();
    expect(screen.getByText("settings.tabs.solidatus")).toBeInTheDocument();
    expect(screen.getByText("settings.tabs.collibra")).toBeInTheDocument();
  });

  it("hides both integration tabs when the server denies authoring (readOnly=true)", () => {
    // Local role is irrelevant to this gate now — even a role string that
    // used to pass the coarse check must not bypass a server denial.
    localStorage.setItem("user_role", "member");
    authState.canAuthor = false;
    renderPanel();
    expect(screen.queryByText("settings.tabs.solidatus")).not.toBeInTheDocument();
    expect(screen.queryByText("settings.tabs.collibra")).not.toBeInTheDocument();
  });

  it("hides both integration tabs for a viewer (no authoring binding)", () => {
    localStorage.setItem("user_role", "viewer");
    authState.canAuthor = false;
    renderPanel();
    expect(screen.queryByText("settings.tabs.solidatus")).not.toBeInTheDocument();
    expect(screen.queryByText("settings.tabs.collibra")).not.toBeInTheDocument();
  });

  it("shows both integration tabs for a tenant admin (readOnly=false)", () => {
    localStorage.setItem("user_role", "tenant_admin");
    authState.canAuthor = true;
    renderPanel();
    expect(screen.getByText("settings.tabs.solidatus")).toBeInTheDocument();
    expect(screen.getByText("settings.tabs.collibra")).toBeInTheDocument();
  });

  it("never mounts the Collibra/Solidatus panel content when authoring is denied, even if that tab was previously selected", () => {
    authState.canAuthor = false;
    renderPanel();
    // The gate gap this closes: the tab is unreachable, so its panel (which
    // renders admin/modeler mutation controls) never mounts either.
    expect(screen.queryByTestId("collibra-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("solidatus-panel")).not.toBeInTheDocument();
  });
});
