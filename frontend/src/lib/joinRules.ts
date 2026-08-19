/**
 * Centralised rules governing what kinds of joins are expected in a model.
 *
 * The backend enforces one-fact-per-model at the table layer (see
 * model-service/src/api/tables._assert_at_most_one_fact) but does not reject
 * same-type joins, which the canvas treats as structurally unusual rather
 * than invalid. The canvas consults these helpers before opening the join
 * dialog so the user sees consistent messaging regardless of how the drag
 * terminated (on a handle, in the node interior, or loosely).
 */

export type TableTypeish = string | undefined | null;

/**
 * Whether a table_type value is a dimension type. The API domain is
 * "fact" | "dim_aggregate" | "dim_detail" | "unclassified" | "calendar";
 * a table is a dimension if its type starts with "dim" (Bug-7635).
 * JoinsPanel already used this pattern inline -- centralised here so
 * Canvas edge-data construction and the same-type warning share one rule.
 */
export function isDimTableType(t: TableTypeish): boolean {
  if (!t) return false;
  return t.toLowerCase().startsWith("dim");
}

export interface JoinCombinationCheck {
  /** true when the pair is the canonical fact<->dim edge. */
  isFactDim: boolean;
  /** true when both endpoints share a table_type (fact<->fact or dim<->dim). */
  isSameType: boolean;
  /** Human-readable label for the same-type combination, or null. */
  sameTypeLabel: string | null;
}

export function classifyJoinEndpoints(
  left: TableTypeish,
  right: TableTypeish,
  t?: (key: string, vars?: Record<string, string>) => string,
): JoinCombinationCheck {
  const l = (left ?? "").toLowerCase();
  const r = (right ?? "").toLowerCase();
  const lIsDim = isDimTableType(l);
  const rIsDim = isDimTableType(r);
  const isFactDim =
    (l === "fact" && r !== "fact" && r !== "") ||
    (r === "fact" && l !== "fact" && l !== "");
  // Same-type: both fact, or both dim (even if different dim subtypes like
  // dim_detail and dim_aggregate -- those are still dimension-to-dimension).
  const isSameType =
    (l !== "" && l === r) || (lIsDim && rIsDim);
  let sameTypeLabel: string | null = null;
  if (isSameType) {
    // Translatable labels for the two same-type combinations the canvas can
    // produce (F-026-12). Falls back to a plain English template only when no
    // `t` is supplied (non-React call sites / tests).
    if (l === "fact") {
      sameTypeLabel = t ? t("joins.factToFact") : "fact-to-fact";
    } else if (lIsDim && rIsDim) {
      sameTypeLabel = t ? t("joins.dimensionToDimension") : "dimension-to-dimension";
    } else {
      // Any other matching type (defensive).
      sameTypeLabel = t ? t("joins.sameTypeGeneric", { type: l }) : `${l}-to-${l}`;
    }
  }
  return { isFactDim, isSameType, sameTypeLabel };
}

/**
 * Whether a join_type denotes an outer join (left / right / full) and should
 * therefore render with a dashed edge. The API stores short names
 * ("inner" | "left" | "right" | "full"), none of which contain the substring
 * "outer" -- the previous `includes("outer")` test was always false, so outer
 * joins never rendered dashed (F-026-05). Centralised here so the edge and any
 * other consumer share one definition.
 */
const OUTER_JOIN_TYPES = new Set(["left", "right", "full"]);

export function isOuterJoinType(joinType: TableTypeish): boolean {
  if (!joinType) return false;
  return OUTER_JOIN_TYPES.has(joinType.toLowerCase().trim());
}

export function sameTypeWarningText(label: string, t?: (key: string, vars?: Record<string, string>) => string): string {
  const key = "joins.unusualJoinWarning";
  return t
    ? t(key, { label })
    : `Unusual join: ${label}. Tessallite models expect fact-to-dimension edges; review before saving.`;
}
