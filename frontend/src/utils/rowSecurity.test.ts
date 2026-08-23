/**
 * Bug-8453 — the Query Panel must distinguish "row security denied every row"
 * from "there is genuinely no data", instead of rendering both as an empty grid.
 *
 * Guards the classification helper the panel branches on. It deliberately does
 * NOT key on `rows.length === 0`: a deny-all rewrites the query to
 * `... WHERE 0 = 1`, over which a COUNT-shaped query still returns a row
 * containing 0.
 */
import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import {
  ROW_SECURITY_DENY_ALL_RULE_ID,
  rowSecurityDeniedAll,
  rowSecurityNarrowed,
  securityRulesApplied,
} from "./rowSecurity";

describe("securityRulesApplied", () => {
  it("reads the execute-contract field", () => {
    expect(
      securityRulesApplied({ security_rules_applied: ["r1", "r2"] }),
    ).toEqual(["r1", "r2"]);
  });

  it("degrades to empty on a missing, null or malformed field", () => {
    expect(securityRulesApplied(null)).toEqual([]);
    expect(securityRulesApplied(undefined)).toEqual([]);
    expect(securityRulesApplied({})).toEqual([]);
    expect(securityRulesApplied({ security_rules_applied: null })).toEqual([]);
    expect(securityRulesApplied({ security_rules_applied: "r1" })).toEqual([]);
    expect(securityRulesApplied({ security_rules_applied: 7 })).toEqual([]);
  });

  it("drops non-string and empty entries", () => {
    expect(
      securityRulesApplied({ security_rules_applied: ["r1", "", null, 3] }),
    ).toEqual(["r1"]);
  });
});

describe("rowSecurityDeniedAll", () => {
  it("recognises the deny-all sentinel", () => {
    expect(
      rowSecurityDeniedAll({
        security_rules_applied: [ROW_SECURITY_DENY_ALL_RULE_ID],
      }),
    ).toBe(true);
  });

  it("does not treat a narrowing rule as a denial", () => {
    // The rows shown ARE correct, just scoped to the caller. Reporting this as
    // a denial would tell every restricted user they can see nothing.
    expect(
      rowSecurityDeniedAll({ security_rules_applied: ["region-rule"] }),
    ).toBe(false);
    expect(
      rowSecurityNarrowed({ security_rules_applied: ["region-rule"] }),
    ).toBe(true);
  });

  it("does not infer a denial from an empty result", () => {
    expect(
      rowSecurityDeniedAll({ rows: [], security_rules_applied: [] } as never),
    ).toBe(false);
    expect(rowSecurityDeniedAll(null)).toBe(false);
  });

  it("detects a denial even when a row was returned", () => {
    // COUNT(*) over `WHERE 0 = 1` returns a row containing 0 — a grid with one
    // row that must still be badged as restricted.
    expect(
      rowSecurityDeniedAll({
        rows: [{ c: 0 }],
        security_rules_applied: [ROW_SECURITY_DENY_ALL_RULE_ID],
      } as never),
    ).toBe(true);
  });

  it("detects a denial alongside other applied rules", () => {
    expect(
      rowSecurityDeniedAll({
        security_rules_applied: ["region-rule", ROW_SECURITY_DENY_ALL_RULE_ID],
      }),
    ).toBe(true);
    expect(
      rowSecurityNarrowed({
        security_rules_applied: ["region-rule", ROW_SECURITY_DENY_ALL_RULE_ID],
      }),
    ).toBe(false);
  });
});

describe("mirror integrity", () => {
  it("pins the sentinel to shared/security/execute_contract.py", () => {
    // R2 review S6: rowSecurity.ts is a MIRROR of the Python contract (the
    // browser cannot import `shared`). The MCP mirror has this guard; this one
    // did not — two mirrors of one constant, only one of them protected.
    const shared = path.resolve(
      __dirname, "../../../shared/security/execute_contract.py",
    );
    expect(fs.existsSync(shared)).toBe(true);
    const text = fs.readFileSync(shared, "utf-8");
    const m = text.match(/^ROW_SECURITY_DENY_ALL_RULE_ID\s*=\s*"([^"]+)"/m);
    expect(m).not.toBeNull();
    expect(m![1]).toBe(ROW_SECURITY_DENY_ALL_RULE_ID);
  });
});
