/**
 * The `/plugin/execute` row-security contract, Excel add-in side
 * (Bug-8453 / R3 finding S-1).
 *
 * Mirrors `tessallite/shared/security/execute_contract.py`. The add-in is a
 * standalone bundle and cannot import `shared`, so the classification is
 * mirrored here exactly as `frontend/src/utils/rowSecurity.ts` and
 * `mcp-server/src/tools/row_security.py` do.
 *
 * Excel is the surface where an unexplained empty result does the most damage:
 * a business user reads a blank pivot, or a `0` subtotal, as a fact about the
 * business and puts it in a report. Branch on the sentinel, never on
 * `data.length === 0` — a deny-all rewrites the query to `... WHERE 0 = 1`,
 * over which a COUNT-shaped measure still returns a row containing 0.
 */

/** Must equal shared.security.execute_contract.ROW_SECURITY_DENY_ALL_RULE_ID. */
export const ROW_SECURITY_DENY_ALL_RULE_ID = '__deny_all__';

export function securityRulesApplied(
  response: { security_rules_applied?: unknown } | null | undefined,
): string[] {
  const raw = response?.security_rules_applied;
  if (!Array.isArray(raw)) return [];
  return raw.filter((r): r is string => typeof r === 'string' && r.length > 0);
}

/** True when row security denied the caller every row. */
export function rowSecurityDeniedAll(
  response: { security_rules_applied?: unknown } | null | undefined,
): boolean {
  return securityRulesApplied(response).includes(ROW_SECURITY_DENY_ALL_RULE_ID);
}
