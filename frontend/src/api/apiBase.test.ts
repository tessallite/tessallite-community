import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("./systemDefaults", () => ({
  apiBaseOverrideFor: vi.fn(() => null),
}));

import { apiBaseOverrideFor } from "./systemDefaults";
import {
  modelServiceBaseUrl,
  queryRouterBaseUrl,
  optimizerBaseUrl,
  schedulerBaseUrl,
  agentServiceBaseUrl,
} from "./apiBase";

const mockOverride = vi.mocked(apiBaseOverrideFor);

describe("apiBase URL resolution", () => {
  beforeEach(() => {
    mockOverride.mockReturnValue(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe("modelServiceBaseUrl", () => {
    it("returns empty string (same-origin) when no override or env", () => {
      expect(modelServiceBaseUrl()).toBe("");
    });

    it("returns operator override when configured", () => {
      mockOverride.mockReturnValue("https://model.example.com");
      expect(modelServiceBaseUrl()).toBe("https://model.example.com");
    });
  });

  describe("queryRouterBaseUrl", () => {
    it("returns /query-router relative path by default", () => {
      expect(queryRouterBaseUrl()).toBe("/query-router");
    });

    it("returns operator override when configured", () => {
      mockOverride.mockReturnValue("https://qr.example.com");
      expect(queryRouterBaseUrl()).toBe("https://qr.example.com");
    });
  });

  describe("optimizerBaseUrl", () => {
    it("returns /optimizer relative path by default", () => {
      expect(optimizerBaseUrl()).toBe("/optimizer");
    });

    it("returns operator override when configured", () => {
      mockOverride.mockReturnValue("https://opt.example.com");
      expect(optimizerBaseUrl()).toBe("https://opt.example.com");
    });
  });

  describe("schedulerBaseUrl", () => {
    it("returns /scheduler relative path by default", () => {
      expect(schedulerBaseUrl()).toBe("/scheduler");
    });
  });

  describe("agentServiceBaseUrl", () => {
    it("returns /agent relative path by default", () => {
      expect(agentServiceBaseUrl()).toBe("/agent");
    });
  });

  it("override always wins over env var and relative path", () => {
    mockOverride.mockReturnValue("https://override.example.com");
    expect(queryRouterBaseUrl()).toBe("https://override.example.com");
    expect(optimizerBaseUrl()).toBe("https://override.example.com");
    expect(schedulerBaseUrl()).toBe("https://override.example.com");
  });
});
