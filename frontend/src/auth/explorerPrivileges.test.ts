import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { canPerform, type ExplorerAction } from "./explorerPrivileges";

/**
 * Compliance matrix for Explorer UI gating. Each action is checked for the
 * three personas the product enforces — Viewer, Modeller, Tenant Admin — plus
 * system_admin (inherits tenant_admin), the legacy aliases, and logged-out
 * state. This is the accepted/rejected truth table; it must stay in lockstep
 * with docs/architecture/architecture_explorer-rbac-matrix.md and the backend
 * require_role gates.
 */

const TENANT_ADMIN_ACTIONS: ExplorerAction[] = [
  "project.create",
  "project.importExport",
  "project.toggleActive",
  "project.delete",
];

const MODELLER_ACTIONS: ExplorerAction[] = [
  "project.rename",
  "project.configDrawer",
  "model.add",
  "model.rename",
  "model.delete",
  "model.importExport",
  "model.deploy",
];

function setRole(role: string | null) {
  if (role === null) localStorage.removeItem("user_role");
  else localStorage.setItem("user_role", role);
}

describe("explorerPrivileges.canPerform", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  describe("tenant-admin-only actions", () => {
    it.each(TENANT_ADMIN_ACTIONS)("'%s' is allowed for tenant_admin", (action) => {
      setRole("tenant_admin");
      expect(canPerform(action)).toBe(true);
    });

    it.each(TENANT_ADMIN_ACTIONS)("'%s' is allowed for system_admin (inherits)", (action) => {
      setRole("system_admin");
      expect(canPerform(action)).toBe(true);
    });

    it.each(TENANT_ADMIN_ACTIONS)("'%s' is REJECTED for modeller", (action) => {
      setRole("modeler");
      expect(canPerform(action)).toBe(false);
    });

    it.each(TENANT_ADMIN_ACTIONS)("'%s' is REJECTED for viewer", (action) => {
      setRole("viewer");
      expect(canPerform(action)).toBe(false);
    });

    it.each(TENANT_ADMIN_ACTIONS)("'%s' is REJECTED when logged out", (action) => {
      setRole(null);
      expect(canPerform(action)).toBe(false);
    });
  });

  describe("modeller-tier actions", () => {
    it.each(MODELLER_ACTIONS)("'%s' is allowed for modeller", (action) => {
      setRole("modeler");
      expect(canPerform(action)).toBe(true);
    });

    it.each(MODELLER_ACTIONS)("'%s' is allowed for tenant_admin (inherits)", (action) => {
      setRole("tenant_admin");
      expect(canPerform(action)).toBe(true);
    });

    it.each(MODELLER_ACTIONS)("'%s' is allowed for system_admin (inherits)", (action) => {
      setRole("system_admin");
      expect(canPerform(action)).toBe(true);
    });

    it.each(MODELLER_ACTIONS)("'%s' is REJECTED for viewer", (action) => {
      setRole("viewer");
      expect(canPerform(action)).toBe(false);
    });

    it.each(MODELLER_ACTIONS)("'%s' is REJECTED when logged out", (action) => {
      setRole(null);
      expect(canPerform(action)).toBe(false);
    });

    // member / analyst / model_technical are NOT editor tiers (F-021-12).
    it.each(["member", "analyst", "model_technical"])(
      "modeller actions are REJECTED for non-editor alias %s",
      (role) => {
        setRole(role);
        for (const action of MODELLER_ACTIONS) {
          expect(canPerform(action)).toBe(false);
        }
      },
    );
  });

  describe("admin inherits every modeller action", () => {
    it("tenant_admin can do everything a modeller can", () => {
      setRole("modeler");
      const modellerAllowed = MODELLER_ACTIONS.filter((a) => canPerform(a));
      setRole("tenant_admin");
      for (const action of modellerAllowed) {
        expect(canPerform(action)).toBe(true);
      }
    });
  });
});
