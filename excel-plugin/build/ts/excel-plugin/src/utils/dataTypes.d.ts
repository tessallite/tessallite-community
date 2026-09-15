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
export declare function classifyDataType(physicalType: string | null | undefined): SemanticTypeCategory;
/**
 * True when the physical type is a textual/categorical type suitable as a
 * grouping dimension (top-n, geographic, category breakdowns).
 */
export declare function isTextualType(physicalType: string | null | undefined): boolean;
