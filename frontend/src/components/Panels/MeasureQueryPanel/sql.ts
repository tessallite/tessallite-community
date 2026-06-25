/**
 * Pivot SQL builder for the MeasureQueryPanel.
 *
 * Architectural note (Bug-907): SQL produced here uses ANSI/PostgreSQL-style
 * double-quoted identifiers and standard SQL syntax.  It is sent to the
 * query-router as ``raw_query`` with ``dialect: "postgresql"``.  The
 * query-router parses, binds against the semantic model, and rewrites to
 * the target source dialect before execution.  Dialect-specific syntax
 * (BigQuery backticks, SQL Server bracket quoting, etc.) must NOT be
 * emitted here — the router is the single point of dialect translation.
 */
import type { Dimension, Model } from "../../../api/types";
import type { Slicer } from "./types";
import type { PivotColumnMeasure } from "./measureColumns";

/**
 * Tokens that must not appear in a scratchpad expression.  These prevent
 * raw SQL injection through freeform measure expressions that are interpolated
 * into the grouped pivot query (Bug-5315).  The query-router is the ultimate
 * gatekeeper but catching obvious hazards here avoids confusing backend errors.
 */
const DANGEROUS_TOKENS = /\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE)\b/i;
const DANGEROUS_CHARS = /[;]/;

/** Returns an error string if the expression is unsafe, null otherwise. */
export function validateScratchpadExpression(expr: string): string | null {
  if (!expr || !expr.trim()) return "Expression is empty";
  if (DANGEROUS_CHARS.test(expr)) return "Expression contains disallowed characters (;)";
  if (DANGEROUS_TOKENS.test(expr)) return "Expression contains disallowed SQL keywords";
  // Reject unbalanced parentheses
  let depth = 0;
  for (const ch of expr) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (depth < 0) return "Unbalanced parentheses in expression";
  }
  if (depth !== 0) return "Unbalanced parentheses in expression";
  return null;
}

function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

function quoteValue(v: string): string {
  return `'${v.replace(/'/g, "''")}'`;
}

function slicerToSql(dim: Dimension, s: Slicer): string | null {
  const col = quoteIdent(dim.name);
  switch (s.op) {
    case "eq":
      if (s.values.length === 0) return null;
      return `${col} = ${quoteValue(s.values[0])}`;
    case "ne":
      if (s.values.length === 0) return null;
      return `${col} <> ${quoteValue(s.values[0])}`;
    case "gt":
      if (s.values.length === 0 || !s.values[0]) return null;
      return `${col} > ${quoteValue(s.values[0])}`;
    case "gte":
      if (s.values.length === 0 || !s.values[0]) return null;
      return `${col} >= ${quoteValue(s.values[0])}`;
    case "lt":
      if (s.values.length === 0 || !s.values[0]) return null;
      return `${col} < ${quoteValue(s.values[0])}`;
    case "lte":
      if (s.values.length === 0 || !s.values[0]) return null;
      return `${col} <= ${quoteValue(s.values[0])}`;
    case "in": {
      const vs = s.values.filter((v) => v.length > 0);
      if (vs.length === 0) return null;
      return `${col} IN (${vs.map(quoteValue).join(", ")})`;
    }
    case "between":
      if (s.values.length < 2 || !s.values[0] || !s.values[1]) return null;
      return `${col} BETWEEN ${quoteValue(s.values[0])} AND ${quoteValue(s.values[1])}`;
    case "like":
      if (s.values.length === 0 || !s.values[0]) return null;
      return `${col} LIKE ${quoteValue(s.values[0])}`;
    case "is_null":
      return `${col} IS NULL`;
    case "is_not_null":
      return `${col} IS NOT NULL`;
  }
}

export function buildPivotSql(
  model: Model,
  columns: PivotColumnMeasure[],
  rowDims: Dimension[],
  colDims: Dimension[],
  slicers: Slicer[] = [],
  slicerDims: Dimension[] = [],
): string {
  const slug = model.slug;
  const groupNames: string[] = [];
  const seen = new Set<string>();
  for (const d of [...rowDims, ...colDims]) {
    if (seen.has(d.id)) continue;
    seen.add(d.id);
    groupNames.push(d.name);
  }
  const selectCols = groupNames.map(quoteIdent);
  for (const m of columns) {
    const alias = quoteIdent(m._alias);
    if (m._recordCount) {
      selectCols.push(`COUNT(*) AS ${alias}`);
    } else if (m._scratchpad && m.expression) {
      // Guard: reject dangerous expressions before interpolation (Bug-5315).
      const exprError = validateScratchpadExpression(m.expression);
      if (exprError) {
        selectCols.push(`NULL AS ${alias} /* ${quoteIdent(exprError)} */`);
      } else {
        selectCols.push(`(${m.expression}) AS ${alias}`);
      }
    } else {
      const agg = (m._agg || m.default_agg || "SUM").toUpperCase();
      const aggCol = agg === "COUNT_DISTINCT"
        ? `COUNT(DISTINCT ${quoteIdent(m._baseName)})`
        : `${agg}(${quoteIdent(m._baseName)})`;
      selectCols.push(`${aggCol} AS ${alias}`);
    }
  }
  let sql = `SELECT ${selectCols.join(", ")} FROM ${quoteIdent(slug)}`;

  const dimsById = new Map<string, Dimension>();
  for (const d of slicerDims) dimsById.set(d.id, d);
  const predicates: string[] = [];
  for (const s of slicers) {
    const dim = dimsById.get(s.dimensionId);
    if (!dim) continue;
    const pred = slicerToSql(dim, s);
    if (pred) predicates.push(pred);
  }
  if (predicates.length > 0) {
    sql += ` WHERE ${predicates.join(" AND ")}`;
  }
  if (groupNames.length > 0) {
    sql += ` GROUP BY ${groupNames.map(quoteIdent).join(", ")}`;
  }
  return sql;
}
