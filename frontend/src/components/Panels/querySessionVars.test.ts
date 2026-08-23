import { describe, expect, it } from "vitest";
import { serializeSessionVarValue, syncPublishedSessionVars } from "./querySessionVars";

describe("Bug-9224 published session variables", () => {
  it("preserves structured defaults in the resolver's JSON wire format", () => {
    expect(
      serializeSessionVarValue({ from: "2026-01-01", to: "2026-01-31" }),
    ).toBe('{"from":"2026-01-01","to":"2026-01-31"}');
    expect(serializeSessionVarValue(["New York, NY", "Paris"])).toBe(
      '["New York, NY","Paris"]',
    );
    expect(serializeSessionVarValue("already-encoded")).toBe("already-encoded");
  });

  it("sends only published keys and copies defaults under their exact key", () => {
    expect(
      syncPublishedSessionVars(
        { "app.oldname": "stale", "app.region": "manual" },
        [
          {
            session_var_key: "app.region",
            has_default: true,
            default_value: "EMEA",
            sql_usable: true,
          },
          {
            session_var_key: "app.asofdate",
            has_default: true,
            default_value: { from: "2026-01-01", to: "2026-01-31" },
            sql_usable: true,
          },
        ],
      ),
    ).toEqual({
      "app.region": "manual",
      "app.asofdate": '{"from":"2026-01-01","to":"2026-01-31"}',
    });
  });

  it("R2-PCR-002: drops colliding rows marked sql_usable=false", () => {
    expect(
      syncPublishedSessionVars(
        { "app.region": "EMEA", "app.legacy.region": "APAC" },
        [
          {
            session_var_key: "app.region",
            has_default: true,
            default_value: "EMEA",
            sql_usable: false,
          },
          {
            session_var_key: "app.legacy.region",
            has_default: true,
            default_value: "APAC",
            sql_usable: false,
          },
        ],
      ),
    ).toEqual({});
  });
});
