import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { isNonContentModelWrite } from "./client";

describe("client CSRF and auth helpers", () => {
  beforeEach(() => {
    Object.defineProperty(document, "cookie", {
      writable: true,
      value: "",
    });
    localStorage.clear();
  });

  afterEach(() => {
    Object.defineProperty(document, "cookie", {
      writable: true,
      value: "",
    });
    localStorage.clear();
  });

  describe("getCsrfToken extraction", () => {
    it("extracts csrf_token from cookies", () => {
      document.cookie = "other=abc; csrf_token=test-csrf-value; session=xyz";
      const token = document.cookie
        .split("; ")
        .find((row) => row.startsWith("csrf_token="))
        ?.split("=")[1];
      expect(token).toBe("test-csrf-value");
    });

    it("returns undefined when no csrf_token cookie", () => {
      document.cookie = "other=abc; session=xyz";
      const token = document.cookie
        .split("; ")
        .find((row) => row.startsWith("csrf_token="))
        ?.split("=")[1];
      expect(token).toBeUndefined();
    });

    it("handles single cookie", () => {
      document.cookie = "csrf_token=only-one";
      const token = document.cookie
        .split("; ")
        .find((row) => row.startsWith("csrf_token="))
        ?.split("=")[1];
      expect(token).toBe("only-one");
    });
  });

  describe("MODEL_WRITE_RE pattern", () => {
    const MODEL_WRITE_RE =
      /\/api\/v1\/projects\/[^/]+\/models\/(?!snapshot-export|snapshot-import)([^/]+)(\/(?!versions|deploy|undeploy|export|import|snapshot-export|snapshot-import).*)?$/;

    it("matches model sub-resource writes", () => {
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/dimensions")).toBe(true);
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/measures")).toBe(true);
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/joins")).toBe(true);
    });

    it("matches model-level writes", () => {
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1")).toBe(true);
    });

    it("excludes version/deploy/export operations", () => {
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/versions")).toBe(false);
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/deploy")).toBe(false);
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/undeploy")).toBe(false);
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/export")).toBe(false);
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/m1/import")).toBe(false);
    });

    it("excludes snapshot operations", () => {
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/snapshot-export")).toBe(false);
      expect(MODEL_WRITE_RE.test("/api/v1/projects/p1/models/snapshot-import")).toBe(false);
    });

    it("captures the model ID", () => {
      const match = MODEL_WRITE_RE.exec("/api/v1/projects/p1/models/abc-123/dimensions");
      expect(match?.[1]).toBe("abc-123");
    });
  });

  // F-026-11: layout-only and status-only bare-model PATCHes are NOT content
  // edits and must not mark the model dirty (which would block Deploy and
  // force a pointless version). Content writes still mark dirty.
  describe("isNonContentModelWrite (F-026-11)", () => {
    it("classifies a layout-only bare-model PATCH as non-content", () => {
      expect(
        isNonContentModelWrite("PATCH", false, JSON.stringify({ canvas_layout: { tables: {} } })),
      ).toBe(true);
    });

    it("classifies a status-only bare-model PATCH as non-content", () => {
      expect(isNonContentModelWrite("PATCH", false, JSON.stringify({ status: "disabled" }))).toBe(true);
    });

    it("treats a PATCH that also touches content (display_name) as content", () => {
      expect(
        isNonContentModelWrite("PATCH", false, JSON.stringify({ status: "active", display_name: "X" })),
      ).toBe(false);
    });

    it("treats any sub-resource write as content even with a layout-shaped body", () => {
      // hasSubResourcePath true -> always content.
      expect(isNonContentModelWrite("PATCH", true, JSON.stringify({ canvas_layout: {} }))).toBe(false);
    });

    it("treats a non-PATCH write (POST) as content", () => {
      expect(isNonContentModelWrite("POST", false, JSON.stringify({ canvas_layout: {} }))).toBe(false);
    });

    it("treats an empty / unparseable body as content (safe default)", () => {
      expect(isNonContentModelWrite("PATCH", false, "{}")).toBe(false);
      expect(isNonContentModelWrite("PATCH", false, "not json")).toBe(false);
      expect(isNonContentModelWrite("PATCH", false, undefined)).toBe(false);
    });
  });
});
