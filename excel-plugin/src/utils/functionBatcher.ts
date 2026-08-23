/**
 * Coalescing batcher for TESSALLITE.* custom functions (Phase A).
 *
 * A sheet with 200 TESSALLITE.VALUE() cells must not issue 200 REST calls.
 * This module collects invocations for ~50 ms, groups them by (model,
 * filter-column-shape), issues one plugin-protocol query per group, and fans
 * results back out to the pending promises.
 *
 * WRONG-NUMBERS GUARD: when multiple invocations share the same filter COLUMN
 * but different VALUES (e.g., region=EU vs region=US), the query includes the
 * filter column as a dimension (GROUP BY), returning one row per member value.
 * Each invocation's promise receives its own row's value — never another
 * member's number.
 *
 * Pure + side-effect-free (apart from the timer). The actual REST call is
 * injected via `executeBatch` so the batcher is unit-testable without a
 * running server.
 */

/** A single pending invocation waiting to be batched. */
export interface PendingInvocation {
  model: string;
  measure: string;
  /** Filter pairs as [column, value] tuples; empty array = no filters. */
  filters: [string, string][];
  resolve: (value: number | string | null) => void;
  reject: (error: Error) => void;
}

/**
 * The shape key groups invocations that share the same model and the same set
 * of filter COLUMNS (but possibly different filter VALUES).
 *
 * Shape = `${model}::${sorted filter column names joined by "|"}`.
 */
export function computeShapeKey(model: string, filters: [string, string][]): string {
  const cols = filters.map(f => f[0]).sort();
  return `${model}::${cols.join('|')}`;
}

/**
 * Bug-7394: normalize a member value for key comparison. Trims whitespace,
 * case-folds, strips a trailing 'T00:00:00' ISO suffix (dates serialized
 * with time-of-day vs bare date), and drops trailing '.0' on integers
 * serialized as floats (e.g. `5.0` vs `5`). This ensures the user-typed
 * filter value and the server-returned member value produce the same key.
 */
export function normalizeMemberValue(val: string): string {
  let v = val.trim().toLowerCase();
  // ISO date normalization: 2024-01-15T00:00:00 -> 2024-01-15
  v = v.replace(/t00:00:00(?:\.0+)?(?:z)?$/, '');
  // Numeric: strip trailing .0 (5.0 -> 5, 12.00 -> 12)
  if (/^-?\d+\.0+$/.test(v)) {
    v = v.replace(/\.0+$/, '');
  }
  return v;
}

/**
 * Bug-7394 (adversarial R2 finding): encode a sorted list of (col, value)
 * tuples UNAMBIGUOUSLY so no member value can forge a delimiter boundary.
 *
 * The earlier `col=value` + `|`-join scheme was injectable: a member value
 * containing `=` or `|` (composite keys, URL/query-string-like values, tag
 * strings) could make two GENUINELY DISTINCT tuples serialize to the same
 * string — forging an identical result key and collision signature, which
 * silently delivered one member's number to another's cell. JSON.stringify of
 * the sorted `[col, value]` pair array cannot be forged: every string is
 * quote-delimited and internal quotes/backslashes are escaped, so distinct
 * inputs always yield distinct output.
 */
function encodePairs(pairs: [string, string][]): string {
  // Sort by the encoded pair so ordering is deterministic and itself
  // injection-proof (sorting raw `col` then `value` is fine — the JSON
  // encoding downstream is what guarantees non-forgeability).
  const sorted = [...pairs].sort((a, b) => {
    if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
    return a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0;
  });
  return JSON.stringify(sorted);
}

/**
 * Build a unique result key for a single invocation so the fan-out can find
 * it in the batch result map.
 *
 * Key = `${measure}::${JSON of sorted [col, normalizedValue] pairs}`.
 *
 * Bug-7394: both sides (user input and server response) are normalized so
 * lexical differences in date/numeric serialization do not cause #N/A; the
 * pairs are JSON-encoded so no value can forge a delimiter boundary (R2 fix).
 */
export function computeResultKey(measure: string, filters: [string, string][]): string {
  const pairs: [string, string][] = filters.map(f => [f[0], normalizeMemberValue(f[1])]);
  return `${measure}::${encodePairs(pairs)}`;
}

/**
 * Bug-7394 (adversarial R1 finding): a collision-safe accumulator for the
 * producer's GROUP BY result map.
 *
 * Normalization (`normalizeMemberValue`) exists ONLY to bridge user-typed vs
 * server-serialized forms of the SAME member; it must never merge two DISTINCT
 * members. But two distinct server rows (e.g. `region="EU"` and `region="eu"`,
 * or `code="5"` and `code="5.0"`, or separate bare-date and `T00:00:00` rows)
 * normalize to the same fan-out key. Overwriting one with the other silently
 * delivers a WRONG NUMBER to a cell.
 *
 * This accumulator tracks, per normalized key, the RAW member signature that
 * produced it. A second row with the same key but a DIFFERENT raw signature is
 * a genuine collision: the key is POISONED so `finalize()` drops it, and the
 * consumer's missing-key path resolves it to null -> #N/A — the safe
 * pre-normalization degradation, never a wrong number.
 */
export class CollisionSafeResultMap {
  private readonly values = new Map<string, number | string | null>();
  private readonly rawByKey = new Map<string, string>();
  private readonly poisoned = new Set<string>();

  /**
   * Build the RAW member signature for a row (RAW server values, NOT
   * normalized), so two distinct members never share a signature.
   *
   * Bug-7394 (adversarial R2): uses the same JSON encoding as computeResultKey
   * so a member value containing `=`/`|`/quotes cannot forge a signature that
   * collides with a different tuple. Without this, distinct members with
   * delimiter-bearing values shared a signature and the poison never fired,
   * re-opening the wrong-number path.
   */
  static rawSignature(rawFilters: [string, string][]): string {
    return encodePairs(rawFilters);
  }

  /**
   * Record a value under `key`. `rawSignature` identifies the concrete server
   * member. Same signature again = idempotent (kept). Different signature under
   * an already-set key = distinct members collided -> poison the key.
   */
  set(key: string, rawSignature: string, value: number | string | null): void {
    if (this.poisoned.has(key)) return;
    const existingRaw = this.rawByKey.get(key);
    if (existingRaw === undefined) {
      this.rawByKey.set(key, rawSignature);
      this.values.set(key, value);
      return;
    }
    if (existingRaw === rawSignature) {
      this.values.set(key, value);
      return;
    }
    // Distinct members collapsed to one normalized key: ambiguous — drop it.
    this.poisoned.add(key);
    this.values.delete(key);
  }

  /**
   * The plain fan-out map for the batcher: poisoned (ambiguous) keys are
   * OMITTED, so the consumer's `results.has(key) ? get : null` path yields
   * null (#N/A) for them.
   */
  finalize(): Map<string, number | string | null> {
    return this.values;
  }
}

/**
 * The executor callback the batcher calls for each shape group.
 *
 * When `dimensionColumns` is non-empty, the executor MUST include those
 * columns as dimensions in the query so the response returns one row per
 * unique member-value tuple (GROUP BY member). The batcher fans each cell
 * its own row's value.
 *
 * `filterValueSets` provides all unique filter-value combinations for
 * the query's IN-list constraint. When only one set exists, a simple
 * equality filter suffices. When multiple sets exist, an IN filter
 * (or no filter, since the dimension is now in GROUP BY) is needed.
 */
export type BatchExecutor = (
  model: string,
  measures: string[],
  filters: [string, string][],
  dimensionColumns: string[],
  filterValueSets: Map<string, Set<string>>,
) => Promise<Map<string, number | string | null>>;

/**
 * A coalescing batcher instance. Create one per custom-functions runtime
 * (typically a module-level singleton). Not shared with the taskpane.
 */
export class FunctionBatcher {
  private pending: PendingInvocation[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly coalesceMs: number;
  private readonly execute: BatchExecutor;

  /**
   * A generation counter that increments on every `invalidate()` call.
   * Batches captured before an invalidation are stale: their results are
   * discarded and their promises REJECTED (Bug-6914 — every enqueued
   * invocation must settle; an unsettled promise is a cell stuck at
   * #GETTING_DATA forever).
   */
  private generation = 0;

  constructor(execute: BatchExecutor, coalesceMs = 50) {
    this.execute = execute;
    this.coalesceMs = coalesceMs;
  }

  /**
   * Enqueue a single function invocation. Returns a promise that resolves
   * with the cell value once the batch completes.
   */
  enqueue(model: string, measure: string, filters: [string, string][]): Promise<number | string | null> {
    return new Promise<number | string | null>((resolve, reject) => {
      this.pending.push({ model, measure, filters, resolve, reject });
      if (!this.timer) {
        this.timer = setTimeout(() => this.flush(), this.coalesceMs);
      }
    });
  }

  /**
   * Invalidate the batcher: any in-flight batch whose results have not yet
   * been delivered is marked stale (results silently dropped), and
   * already-queued-but-not-yet-flushed invocations are rejected so Excel
   * re-evaluates them in a fresh batch.
   */
  invalidate(): void {
    this.generation++;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const stale = this.pending.splice(0);
    for (const inv of stale) {
      inv.reject(new Error('Batcher invalidated — values are being refreshed.'));
    }
  }

  /** Flush all pending invocations, grouped by shape. */
  private async flush(): Promise<void> {
    this.timer = null;
    const batch = this.pending.splice(0);
    if (batch.length === 0) return;

    const capturedGeneration = this.generation;

    // Group by shape key (same model + same filter columns).
    const groups = new Map<string, PendingInvocation[]>();
    for (const inv of batch) {
      const key = computeShapeKey(inv.model, inv.filters);
      const group = groups.get(key);
      if (group) {
        group.push(inv);
      } else {
        groups.set(key, [inv]);
      }
    }

    // Execute each group in parallel.
    const groupPromises: Promise<void>[] = [];
    for (const [, group] of groups) {
      groupPromises.push(this.executeGroup(group, capturedGeneration));
    }
    await Promise.allSettled(groupPromises);
  }

  private async executeGroup(group: PendingInvocation[], capturedGeneration: number): Promise<void> {
    const model = group[0].model;

    // Collect all unique measures.
    const measureSet = new Set<string>();
    for (const inv of group) {
      measureSet.add(inv.measure);
    }
    const measures = [...measureSet];

    // Collect all unique filter-value combinations per column.
    // For each filter column, gather the set of distinct values across
    // all invocations in this group.
    const filterColumns = group[0].filters.map(f => f[0]).sort();
    const filterValueSets = new Map<string, Set<string>>();
    for (const col of filterColumns) {
      filterValueSets.set(col, new Set<string>());
    }
    for (const inv of group) {
      for (const [col, val] of inv.filters) {
        filterValueSets.get(col)?.add(val);
      }
    }

    // Determine which filter columns need to become dimensions (GROUP BY).
    // A column with multiple distinct values must be in the GROUP BY so
    // the response returns one row per member value. A column with a
    // single value is a simple equality filter and does not need GROUP BY.
    const dimensionColumns: string[] = [];
    const singleValueFilters: [string, string][] = [];
    for (const [col, vals] of filterValueSets) {
      if (vals.size > 1) {
        dimensionColumns.push(col);
      } else if (vals.size === 1) {
        singleValueFilters.push([col, [...vals][0]]);
      }
    }

    try {
      const results = await this.execute(
        model,
        measures,
        singleValueFilters,
        dimensionColumns,
        filterValueSets,
      );

      // Bug-6914: if the batcher was invalidated while the request was in
      // flight, the results were computed under the pre-invalidation context
      // and must not be delivered — but the promises MUST still settle.
      // Silently dropping them (the old behaviour) left cells stuck at
      // #GETTING_DATA forever: these invocations were already spliced out of
      // `pending`, so invalidate() can never reach them.
      if (this.generation !== capturedGeneration) {
        const staleError = new Error('Batcher invalidated — values are being refreshed.');
        for (const inv of group) {
          inv.reject(staleError);
        }
        return;
      }

      // Fan results out to the pending promises. Each invocation finds
      // its value by its unique resultKey (measure + filter pairs).
      for (const inv of group) {
        const resultKey = computeResultKey(inv.measure, inv.filters);
        if (results.has(resultKey)) {
          inv.resolve(results.get(resultKey)!);
        } else {
          // The measure+member combination was not in the result — no data.
          inv.resolve(null);
        }
      }
    } catch (e) {
      // Bug-6914: settle the promises on EVERY path — a generation change
      // must not swallow the rejection.
      const error = this.generation !== capturedGeneration
        ? new Error('Batcher invalidated — values are being refreshed.')
        : (e instanceof Error ? e : new Error('Batch execution failed'));
      for (const inv of group) {
        inv.reject(error);
      }
    }
  }

  /** The number of pending (not-yet-flushed) invocations. For testing. */
  get pendingCount(): number {
    return this.pending.length;
  }
}
