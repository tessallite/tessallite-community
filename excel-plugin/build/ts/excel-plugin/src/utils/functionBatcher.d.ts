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
export declare function computeShapeKey(model: string, filters: [string, string][]): string;
/**
 * Bug-7394: normalize a member value for key comparison. Trims whitespace,
 * case-folds, strips a trailing 'T00:00:00' ISO suffix (dates serialized
 * with time-of-day vs bare date), and drops trailing '.0' on integers
 * serialized as floats (e.g. `5.0` vs `5`). This ensures the user-typed
 * filter value and the server-returned member value produce the same key.
 */
export declare function normalizeMemberValue(val: string): string;
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
export declare function computeResultKey(measure: string, filters: [string, string][]): string;
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
export declare class CollisionSafeResultMap {
    private readonly values;
    private readonly rawByKey;
    private readonly poisoned;
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
    static rawSignature(rawFilters: [string, string][]): string;
    /**
     * Record a value under `key`. `rawSignature` identifies the concrete server
     * member. Same signature again = idempotent (kept). Different signature under
     * an already-set key = distinct members collided -> poison the key.
     */
    set(key: string, rawSignature: string, value: number | string | null): void;
    /**
     * The plain fan-out map for the batcher: poisoned (ambiguous) keys are
     * OMITTED, so the consumer's `results.has(key) ? get : null` path yields
     * null (#N/A) for them.
     */
    finalize(): Map<string, number | string | null>;
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
export type BatchExecutor = (model: string, measures: string[], filters: [string, string][], dimensionColumns: string[], filterValueSets: Map<string, Set<string>>) => Promise<Map<string, number | string | null>>;
/**
 * A coalescing batcher instance. Create one per custom-functions runtime
 * (typically a module-level singleton). Not shared with the taskpane.
 */
export declare class FunctionBatcher {
    private pending;
    private timer;
    private readonly coalesceMs;
    private readonly execute;
    /**
     * A generation counter that increments on every `invalidate()` call.
     * Batches captured before an invalidation are stale: their results are
     * discarded and their promises REJECTED (Bug-6914 — every enqueued
     * invocation must settle; an unsettled promise is a cell stuck at
     * #GETTING_DATA forever).
     */
    private generation;
    constructor(execute: BatchExecutor, coalesceMs?: number);
    /**
     * Enqueue a single function invocation. Returns a promise that resolves
     * with the cell value once the batch completes.
     */
    enqueue(model: string, measure: string, filters: [string, string][]): Promise<number | string | null>;
    /**
     * Invalidate the batcher: any in-flight batch whose results have not yet
     * been delivered is marked stale (results silently dropped), and
     * already-queued-but-not-yet-flushed invocations are rejected so Excel
     * re-evaluates them in a fresh batch.
     */
    invalidate(): void;
    /** Flush all pending invocations, grouped by shape. */
    private flush;
    private executeGroup;
    /** The number of pending (not-yet-flushed) invocations. For testing. */
    get pendingCount(): number;
}
