/**
 * The `/execute` row-security contract, frontend side (Bug-8453).
 *
 * Mirrors `shared/security/execute_contract.py`. `security_rules_applied`
 * distinguishes three states that previously rendered identically as an empty
 * grid:
 *
 *  - no rule fired, zero rows      -> genuinely no data
 *  - a rule NARROWED the result    -> the rows shown are correct, scoped to you
 *  - `__deny_all__`                -> you are not permitted to see anything
 *
 * Branch on this, never on `rows.length === 0`: a deny-all rewrites the query
 * to `... WHERE 0 = 1`, over which `COUNT(*)` still returns a row containing 0.
 */

/** Sentinel the router reports when row security denied every row. */
export const ROW_SECURITY_DENY_ALL_RULE_ID = "__deny_all__";

/** Normalise the wire field to a string array; tolerant of null/garbage. */
export function securityRulesApplied(
  response: { security_rules_applied?: unknown } | null | undefined,
): string[] {
  const raw = response?.security_rules_applied;
  if (!Array.isArray(raw)) return [];
  return raw.filter((r): r is string => typeof r === "string" && r.length > 0);
}

/** True when row security denied the caller every row. */
export function rowSecurityDeniedAll(
  response: { security_rules_applied?: unknown } | null | undefined,
): boolean {
  return securityRulesApplied(response).includes(ROW_SECURITY_DENY_ALL_RULE_ID);
}

/** True when row security applied but did not deny everything. */
export function rowSecurityNarrowed(
  response: { security_rules_applied?: unknown } | null | undefined,
): boolean {
  const rules = securityRulesApplied(response);
  return rules.length > 0 && !rules.includes(ROW_SECURITY_DENY_ALL_RULE_ID);
}
