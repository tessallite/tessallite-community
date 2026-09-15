/**
 * Tessallite custom Excel functions.
 *
 * F-025-02: there is exactly ONE published custom-functions namespace,
 * `TESSALLITE`, defined by the `Functions.Namespace` resource in every shipped
 * manifest (`manifest.xml`, `manifest.xml.template`, `sideload-catalog`, and
 * the pane-generated manifest). Office derives the workbook prefix solely from
 * that manifest string; `CustomFunctions.associate('<ID>', fn)` binds the ID,
 * not a second namespace. A separate `TESS.*` namespace was never registered,
 * so `=TESS.KPIVALUE(...)` would resolve to `#NAME?`. All functions below are
 * therefore reachable ONLY under `TESSALLITE.*`; do not reintroduce a `TESS.*`
 * promise in code, metadata, or tests without also publishing (and verifying
 * on a perpetual host) a second manifest namespace.
 *
 * Name-based functions:
 *   TESSALLITE.VALUE(model, measure, [filterCol, filterVal]...)
 *   TESSALLITE.KPI(model, kpiName, property)
 *   TESSALLITE.MEMBERVALUE(model, measure, dimension, member)
 *
 * ID-based functions (retained for backward compatibility, same namespace):
 *   TESSALLITE.LISTBYID, TESSALLITE.KPIVALUE, TESSALLITE.KPIGOAL,
 *   TESSALLITE.KPISTATUS
 */
import { FunctionBatcher } from './utils/functionBatcher';
/**
 * Clear all custom function caches AND invalidate the batcher.
 * Phase A: with shared runtime, this is callable from the taskpane's
 * "Refresh values" action, persona switch, profile switch, and logout.
 *
 * Bug-6912: additionally bumps the cache generation token in
 * OfficeRuntime.storage so the functions runtime (if running in a separate
 * JS context on perpetual Office) detects the invalidation on its next eval.
 *
 * F-025-03: this fire-and-forget form is retained only for teardown paths
 * (logout) where ordering against a persisted governed scope does not matter.
 * For persona/profile/model transitions use `applyContextTransition`, which
 * persists the new scope BEFORE bumping the generation so the separate
 * functions runtime can never observe a new generation with a stale scope.
 */
export declare function clearFunctionCaches(): void;
/**
 * F-025-03: single awaited context-transition operation for persona/profile/
 * model switches. The CALLER must persist the new governed scope (persona,
 * model context, active profile) into OfficeRuntime.storage BEFORE invoking
 * this. This function then, in order:
 *   1. clears this runtime's local caches,
 *   2. awaits the cross-runtime generation bump (so the separate functions
 *      runtime, which reads persona + generation atomically, can only ever
 *      see the NEW generation alongside the already-persisted NEW scope —
 *      never a new generation with the old persona), and
 *   3. requests a full workbook rebuild so already-inserted cells recompute
 *      under the new scope instead of retaining the previous persona's values.
 */
export interface ContextTransitionOptions {
    /** The task-pane generation that owns this transition, when applicable. */
    generation?: number;
    /** False once a newer project/model transition supersedes this one. */
    isCurrent?: () => boolean;
    /** Persist the new governed scope before invalidating any cache. */
    persist?: () => Promise<void>;
}
export declare function applyContextTransition(options?: ContextTransitionOptions): Promise<void>;
/**
 * Refresh values: invalidate caches and trigger a full recalc of all
 * TESSALLITE.* functions. Called by the taskpane's "Refresh values" button.
 * Now awaitable so the caller can sequence UI feedback.
 */
export declare function refreshCustomFunctionValues(): Promise<void>;
/** Bug-6912: exported for testing. Resets the last-seen state. */
export declare function _resetLastSeenGeneration(): void;
/**
 * F6: Build a proper CustomFunctions.Error when available (desktop Excel),
 * falling back to a string for hosts where the constructor is absent.
 * The Error's `code` maps to Excel's error indicator (#CONNECT!, #N/A, etc.)
 * and `message` appears in the cell's tooltip on hover.
 */
/**
 * F6: Build a proper CustomFunctions.Error when the ErrorCode enum is
 * available (desktop Excel), falling back to an error string for other hosts.
 * The Error's `code` maps to Excel's cell error indicator (#CONNECT!, #N/A, etc.)
 * and `message` appears in the cell's tooltip on hover.
 *
 * Unit-testable: in the test environment CustomFunctions.Error is not defined,
 * so the fallback string is returned. Live rendering goes on the manual checklist.
 */
/**
 * F6: Build an error value for a custom function cell.
 *
 * On desktop Excel with CustomFunctions.ErrorCode available, this THROWS a
 * CustomFunctions.Error which Excel catches and renders as a cell error
 * indicator (#N/A, #VALUE!) with the message in the tooltip. The caller
 * must NOT wrap this call in a try/catch that would swallow the CF Error.
 *
 * On hosts without CF Error support (web, tests), returns an error string
 * that Excel renders as the cell text.
 *
 * `code` maps to:
 *   'connect'      -> #N/A + "#CONNECT!" prefix (auth issues)
 *   'notAvailable' -> #N/A (no data)
 *   'invalidValue' -> #VALUE! (bad args)
 *
 * Unit-testable: in the test environment CustomFunctions.Error/ErrorCode are
 * not defined, so the fallback string is returned. Live CF Error rendering
 * goes on the manual checklist.
 */
export declare function makeFunctionError(code: 'connect' | 'notAvailable' | 'invalidValue', message: string): never | string;
declare const valueBatcher: FunctionBatcher;
export { valueBatcher as _valueBatcher };
/**
 * Parse the variadic filter arguments into [column, value] pairs.
 * Filters come in pairs: (filterColumn1, filterValue1, filterColumn2, ...).
 * Missing or empty pairs are skipped.
 */
export declare function parseFilterArgs(...args: (string | undefined | null | boolean)[]): [string, string][];
