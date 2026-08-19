/**
 * Pivot-config error contract (Bug-8161 / Bug-8182 / Bug-7442).
 *
 * The model-service pivot-views endpoints reject an invalid config with HTTP 422
 * whose body carries a machine `error_code` (one of `PivotConfigErrorCode`) plus a
 * human `message`. The panel keys off that `error_code` — never the prose — to show
 * a friendly, translated message, and tucks the raw backend/transport text behind a
 * collapsed accordion so recovery no longer leads with internal detail (Bug-8182).
 *
 * `ERROR_CODE_MAP` keys MUST equal the backend enum string values EXACTLY. That
 * producer/consumer alignment is guarded by `pivotErrors.test.ts`, which reads the
 * canonical token set from `shared/schemas/domains/pivot_config.py`.
 */

type TFunc = (key: string, vars?: Record<string, string | number>) => string;

/**
 * The finite set of machine error codes the backend can return. Kept in the same
 * order as `PivotConfigErrorCode` in the Python schema for easy diffing; the test
 * asserts an exact match against that source regardless of order.
 */
export const PIVOT_ERROR_CODES = [
  "INVALID_STRUCTURE",
  "INVALID_MEASURE_ID",
  "UNKNOWN_MEASURE",
  "INVALID_DIMENSION_ID",
  "UNKNOWN_DIMENSION",
] as const;

export type PivotErrorCode = (typeof PIVOT_ERROR_CODES)[number];

const PIVOT_ERROR_CODE_SET: ReadonlySet<string> = new Set(PIVOT_ERROR_CODES);

/** error_code → i18n message key. Keys equal the backend enum values exactly. */
export const ERROR_CODE_MAP: Record<PivotErrorCode, string> = {
  INVALID_STRUCTURE: "pivot.errorInvalidStructure",
  INVALID_MEASURE_ID: "pivot.errorInvalidMeasureId",
  UNKNOWN_MEASURE: "pivot.errorUnknownMeasure",
  INVALID_DIMENSION_ID: "pivot.errorInvalidDimensionId",
  UNKNOWN_DIMENSION: "pivot.errorUnknownDimension",
};

export interface PivotPanelError {
  /** Friendly, translated message — the primary surface, always shown. */
  message: string;
  /** Raw backend/transport text — shown only behind the collapsed accordion. */
  detail?: string | null;
  /** The machine code when the failure was a typed pivot-config rejection. */
  errorCode?: PivotErrorCode | null;
}

/**
 * Extract the raw prose detail from an axios-style error, falling back to the
 * error message or a caller-supplied string. Shared by the drill path and the
 * untyped branch of `toPivotError` so there is one extraction implementation.
 */
export function extractRawDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (detail) {
    if (typeof detail === "string") return detail;
    if (typeof detail === "object" && detail !== null) {
      const d = detail as Record<string, unknown>;
      if (typeof d.message === "string") return d.message;
      if (typeof d.detail === "string") return d.detail;
      return JSON.stringify(detail);
    }
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

/**
 * Map any pivot load/save/run failure to a `PivotPanelError`.
 *
 * Only a typed 422 whose `detail.error_code` is a PIVOT-CONFIG code (i.e. a member
 * of `PIVOT_ERROR_CODES`) enters the config-specific branch and yields the mapped
 * friendly message. This is the B3 membership gate: the same helper is used for
 * query execution and drill (index.tsx), and the query-router returns its OWN typed
 * codes (`OBJECT_NOT_AVAILABLE`, `PERSONA_OBJECT_NOT_INCLUDED`, `COLUMN_RESTRICTED`,
 * `STABLE_CURSOR_UNAVAILABLE`, ...). Those are NOT pivot-config errors — mapping them
 * to "saved pivot config invalid" would tell an analyst their view is corrupt when
 * they were merely denied by a persona. Every non-pivot code (and every untyped or
 * transport failure) falls through to the caller's generic friendly message, with the
 * raw backend text preserved only behind the collapsed accordion.
 */
export function toPivotError(
  err: unknown,
  t: TFunc,
  fallbackMessage: string,
): PivotPanelError {
  const data = (err as { response?: { data?: unknown } })?.response?.data as
    | { detail?: unknown }
    | undefined;
  const detail = data?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    const code = typeof d.error_code === "string" ? d.error_code : null;
    if (code && PIVOT_ERROR_CODE_SET.has(code)) {
      // A pivot-config code we own — the map is exhaustive over PIVOT_ERROR_CODES.
      const raw = typeof d.message === "string" ? d.message : JSON.stringify(detail);
      return {
        message: t(ERROR_CODE_MAP[code as PivotErrorCode]),
        detail: raw || null,
        errorCode: code as PivotErrorCode,
      };
    }
    // A typed code we do NOT own (query/security) or an object detail with no
    // pivot code: fall through to the generic branch below — never mislabel.
  }
  // Non-pivot / untyped / transport failure: friendly generic message first, raw
  // prose behind the accordion — but never repeat the friendly text as "detail".
  const raw = extractRawDetail(err, "");
  return {
    message: fallbackMessage,
    detail: raw && raw !== fallbackMessage ? raw : null,
    errorCode: null,
  };
}
