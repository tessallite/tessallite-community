import type { DeployedParameterCatalogueItem } from "../../api/types_domains/query_catalogue";

/**
 * Encode a published parameter default on the string-valued session-vars wire.
 *
 * The query-router accepts JSON for structured values such as date_range and
 * JSON arrays for lossless multi_value members. Stringifying an object with
 * String(value) would produce "[object Object]" and silently turn a valid
 * deployed default into an invalid override (Bug-9224).
 */
export function serializeSessionVarValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value) ?? "";
  return String(value);
}

/**
 * Reconcile local overrides with the currently published parameter catalogue.
 *
 * A model switch or deployment refresh can remove a parameter. Keeping its old
 * key would send an override that is not part of the current published
 * contract, so only published session_var_key values survive the refresh.
 *
 * R2-PCR-002: colliding sigil/bare rows are published with ``sql_usable=false``
 * and must not seed or retain session overrides — they are not addressable by
 * supported ``@Name`` placeholders.
 */
export function syncPublishedSessionVars(
  previous: Record<string, string>,
  parameters: readonly Pick<
    DeployedParameterCatalogueItem,
    "session_var_key" | "has_default" | "default_value" | "sql_usable"
  >[],
): Record<string, string> {
  const usable = parameters.filter((parameter) => parameter.sql_usable !== false);
  const publishedKeys = new Set(usable.map((parameter) => parameter.session_var_key));
  const next = Object.fromEntries(
    Object.entries(previous).filter(([key]) => publishedKeys.has(key)),
  );
  for (const parameter of usable) {
    if (next[parameter.session_var_key] === undefined && parameter.has_default) {
      next[parameter.session_var_key] = serializeSessionVarValue(parameter.default_value);
    }
  }
  return next;
}
