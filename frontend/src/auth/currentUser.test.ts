import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  currentUserRole,
  isSystemAdmin,
  isTenantAdmin,
  canEditModelConfig,
} from "./currentUser";

describe("currentUser", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    localStorage.clear();
  });

  describe("currentUserRole", () => {
    it("returns null when no role is stored", () => {
      expect(currentUserRole()).toBeNull();
    });

    it("returns the stored role", () => {
      localStorage.setItem("user_role", "system_admin");
      expect(currentUserRole()).toBe("system_admin");
    });

    it.each(["system_admin", "tenant_admin", "modeler", "viewer", "analyst", "member"] as const)(
      "returns %s when stored",
      (role) => {
        localStorage.setItem("user_role", role);
        expect(currentUserRole()).toBe(role);
      },
    );
  });

  describe("isSystemAdmin", () => {
    it("returns true for system_admin", () => {
      localStorage.setItem("user_role", "system_admin");
      expect(isSystemAdmin()).toBe(true);
    });

    it("returns false for tenant_admin", () => {
      localStorage.setItem("user_role", "tenant_admin");
      expect(isSystemAdmin()).toBe(false);
    });

    it("returns false when no role stored", () => {
      expect(isSystemAdmin()).toBe(false);
    });
  });

  describe("isTenantAdmin", () => {
    it("returns true for system_admin", () => {
      localStorage.setItem("user_role", "system_admin");
      expect(isTenantAdmin()).toBe(true);
    });

    it("returns true for tenant_admin", () => {
      localStorage.setItem("user_role", "tenant_admin");
      expect(isTenantAdmin()).toBe(true);
    });

    it("returns false for modeler", () => {
      localStorage.setItem("user_role", "modeler");
      expect(isTenantAdmin()).toBe(false);
    });

    it("returns false for viewer", () => {
      localStorage.setItem("user_role", "viewer");
      expect(isTenantAdmin()).toBe(false);
    });
  });

  describe("canEditModelConfig", () => {
    it.each(["system_admin", "tenant_admin", "modeler"] as const)(
      "returns true for %s",
      (role) => {
        localStorage.setItem("user_role", role);
        expect(canEditModelConfig()).toBe(true);
      },
    );

    it("returns false for viewer", () => {
      localStorage.setItem("user_role", "viewer");
      expect(canEditModelConfig()).toBe(false);
    });

    it.each(["member", "analyst", "model_technical"] as const)(
      "returns false for non-project-editor alias %s",
      (role) => {
        localStorage.setItem("user_role", role);
        expect(canEditModelConfig()).toBe(false);
      },
    );

    it("returns false when no role stored", () => {
      expect(canEditModelConfig()).toBe(false);
    });
  });
});
