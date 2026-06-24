/**
 * Physical-to-semantic data-type classification (M-2 / Bug-1061).
 *
 * The Report Builder needs to know whether a dimension is textual, numeric, a
 * date, or boolean to drive template auto-fill and gating. The annotation /
 * dimension `data_type` carries the SOURCE's physical type spelling, which
 * differs per warehouse: PostgreSQL says "character varying" / "timestamp
 * without time zone", BigQuery says "STRING" / "INT64", Snowflake says
 * "VARCHAR" / "NUMBER". Comparing `data_type === 'string'` (the canonical
 * semantic word) never matches any of these, so category detection was dead on
 * every real model.
 *
 * This classifier normalises the physical spellings into the four semantic
 * categories the templates reason about. It is client-side DISPLAY/gating
 * logic only — it never builds SQL, so it does no dialect branching for query
 * generation (that stays on the query-router / gateway seams).
 */

export type SemanticTypeCategory = 'text' | 'numeric' | 'date' | 'boolean' | 'unknown';

/**
 * Map a physical data-type string to a semantic category.
 * Matching is case-insensitive and substring-based so length/precision
 * qualifiers ("varchar(255)", "numeric(18,2)", "timestamp without time zone")
 * and warehouse-specific spellings all resolve correctly.
 */
export function classifyDataType(physicalType: string | null | undefined): SemanticTypeCategory {
  if (!physicalType) return 'unknown';
  const t = physicalType.toLowerCase().trim();

  // Boolean — check before numeric ("bit"/"bool" must not fall to numeric).
  if (/\b(bool|boolean|bit)\b/.test(t) || t === 'bool' || t === 'boolean' || t === 'bit') {
    return 'boolean';
  }

  // Date / time — PG "timestamp without time zone", BigQuery DATETIME, etc.
  if (/(date|time|timestamp|datetime)/.test(t)) {
    return 'date';
  }

  // Numeric — PG int/numeric/double precision, BigQuery INT64/FLOAT64/NUMERIC,
  // Snowflake NUMBER, plus money/decimal/real.
  if (/(int|numeric|decimal|float|double|real|number|money|serial)/.test(t)) {
    return 'numeric';
  }

  // Text — PG character varying/text/char, BigQuery STRING, Snowflake VARCHAR,
  // plus the canonical semantic word "string" and uuid/enum-style categoricals.
  if (/(char|text|string|varchar|clob|uuid|enum|name)/.test(t)) {
    return 'text';
  }

  return 'unknown';
}

/**
 * True when the physical type is a textual/categorical type suitable as a
 * grouping dimension (top-n, geographic, category breakdowns).
 */
export function isTextualType(physicalType: string | null | undefined): boolean {
  return classifyDataType(physicalType) === 'text';
}
