/**
 * Measure values at the `/api/v1/plugin/execute` boundary (Bug-9876).
 *
 * The PRODUCER now types its own measure columns: the query-router converts the
 * `Decimal` values the executor returns into JSON numbers before serialising
 * them (`services/query-router/src/api/measure_values.py`), using the same
 * resolved-measure list that names the columns in `annotation.measures`. A
 * measure column therefore arrives as a NUMBER, and a string in a measure
 * column means the value genuinely is text.
 *
 * What survives here is a COMPATIBILITY shim, not the fix. An Office host can
 * hold a cached add-in bundle for a long time and a task pane can point at an
 * older server, so a bundle carrying this code may still meet a query-router
 * that serialises `Decimal` as a string — which is what the owner's live probe
 * on ALEX found: `TESSALLITE.VALUE` cells saved as formula string results
 * (`t="str"`), left-aligned, with `SUM`, number formats and charts treating
 * them as text, and a Report Builder PivotTable that COUNTS instead of summing.
 *
 * Because it is a shim it must be conservative: parsing a string that is not a
 * number is how a client "fix" becomes the next wrong number.
 */

const NUMERIC_TEXT = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

/**
 * Text whose leading zeros carry meaning: a product code, a cost centre, a
 * zip code, an account number. `Number('0042')` is 42, and a measure column
 * holding `'0042'` would silently become the wrong value in a cell. A real
 * `Decimal` never serialises with a leading zero before another digit, so
 * refusing this shape costs the shim nothing.
 */
const SIGNIFICANT_LEADING_ZERO = /^[+-]?0\d/;

/** Number for a measure cell; null for empty; the input unchanged otherwise. */
export function parseMeasureValue(value: unknown): number | null | unknown {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') return value;
  if (typeof value !== 'string') return value;
  const text = value.trim();
  if (!NUMERIC_TEXT.test(text)) return value;
  if (SIGNIFICANT_LEADING_ZERO.test(text)) return value;
  const n = Number(text);
  return Number.isFinite(n) ? n : value;
}

/**
 * Return the rows with every measure column parsed. `measureKeys` comes from
 * `annotation.measures` (preferred) or the requested measure names.
 */
export function normaliseMeasureRows(
  rows: Record<string, unknown>[],
  measureKeys: Iterable<string>,
): Record<string, unknown>[] {
  const keys = [...measureKeys];
  if (keys.length === 0) return rows;
  return rows.map(row => {
    const out: Record<string, unknown> = { ...row };
    for (const k of keys) {
      if (k in out) out[k] = parseMeasureValue(out[k]);
    }
    return out;
  });
}
