/**
 * Measure values at the `/api/v1/plugin/execute` boundary (Bug-9876).
 *
 * The query-router serialises measure values as JSON strings
 * (`{"base_amount":"180720566.17"}` — a Decimal through the JSON encoder), so
 * a consumer that forwards the raw value hands Excel TEXT. The owner's live
 * probe showed `TESSALLITE.VALUE` cells saved as formula string results and a
 * Report Builder PivotTable that would count instead of sum. The response's
 * `annotation.measures` names every measure column, so the parse is exact:
 * measure columns become numbers, dimension columns are untouched.
 */
/** Number for a measure cell; null for empty; the input unchanged otherwise. */
export declare function parseMeasureValue(value: unknown): number | null | unknown;
/**
 * Return the rows with every measure column parsed. `measureKeys` comes from
 * `annotation.measures` (preferred) or the requested measure names.
 */
export declare function normaliseMeasureRows(rows: Record<string, unknown>[], measureKeys: Iterable<string>): Record<string, unknown>[];
