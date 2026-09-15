/**
 * Every message the add-in authors for a CELL, in one place (Bug-9910).
 *
 * The custom-functions runtime cannot show UI. A failure reaches the user as an
 * Excel error cell whose tooltip carries a single line of text, so that line is
 * the entire explanation the user gets.
 *
 * `functions.ts` used to decide which lines were safe to show by matching each
 * message against a hand-maintained list of prefixes. Anything unmatched was
 * replaced with "An error occurred. Check the Tessallite panel for details." —
 * which tells the user nothing, and which the panel does not elaborate on
 * either. That list needed extending every time a message was added, and it was
 * not extended, five times running:
 *
 *   Bug-8712  "Published model unavailable"  — the fail-closed deploy message
 *   Bug-9749  "Request timed out"            — the request ceiling
 *   Bug-9759  the date-variant reason        — a curated server 422
 *   Bug-9880  "Row-level security: ..."      — the deny-all explanation
 *   Bug-9910  "Unknown column: 'x' in ..."   — a refused measure
 *
 * Each was found live, by a user or a harness, as an unexplained cell. So the
 * membership test no longer guesses from the text: a message the add-in wrote
 * is safe BECAUSE it is defined here, and `functions.ts` constructs its errors
 * from these constants. `cellMessageContract.test.ts` fails if a message is
 * constructed anywhere else in the runtime.
 *
 * The strings are English-only by design: this runtime is a classic-script IIFE
 * in an isolated AppContainer that must not pull in the task pane's i18n
 * machinery (see docs/architecture/architecture_excel-custom-functions-runtime.md).
 */

export const CELL_MESSAGES = {
  /** No JWT in OfficeRuntime.storage — the pane has never signed in. */
  notSignedIn: 'Not signed in. Open the Tessallite panel and sign in first.',
  noProfile: 'No active connection profile.',
  noModelSelected:
    'No model selected. Open the Tessallite panel and select a project/model.',
  /**
   * The formula names a model the pane is not connected to. Fails CLOSED: the
   * runtime must never answer a formula with another model's number.
   */
  formulaModelMismatch:
    'Formula model does not match the selected model. Open the Tessallite '
    + 'panel and select the model named in the formula.',

  sessionExpired: 'Session expired. Re-open the Tessallite panel and sign in.',
  accessDenied: 'Access denied.',
  notFound: 'Resource not found.',
  /**
   * Bug-8712: DEPLOYED_SNAPSHOT_INVALID. There is no published definition this
   * function is allowed to serve, and falling back to the live draft is the
   * leak Bug-8384 closed. Naming the remedy is the whole point of the message.
   */
  publishedModelUnavailable:
    'Published model unavailable. Deploy the model again from the model builder.',
  serverError: 'Server error. Try again later.',
  requestFailed: 'Request failed.',
  /**
   * Bug-9749: the wall-clock ceiling on one request. Without it the promise
   * never settles and the cell sits at #GETTING_DATA forever; without the
   * message the user cannot tell a stalled transport from a bad formula.
   */
  requestTimedOut:
    'Request timed out. The Tessallite server did not respond. Check the '
    + 'connection in the Tessallite panel.',
  /**
   * Bug-8453 / Bug-9880: row security denied this caller every row. This is a
   * governed, expected outcome — the cell must say so, because the alternative
   * reading (a business figure of zero) is a wrong number the user would chart
   * and forward.
   */
  rowSecurityDenyAll:
    'Row-level security: your permissions grant you access to no rows for '
    + 'this query. This is a permissions restriction, not a value of zero. '
    + 'Contact your administrator if you believe you should have access.',
  /**
   * The batcher was invalidated mid-flight (Refresh, persona, profile or model
   * switch). Every enqueued invocation must settle — an unsettled promise is a
   * cell stuck at #GETTING_DATA forever (Bug-6914).
   */
  batcherInvalidated: 'Batcher invalidated — values are being refreshed.',
  /** The batch request failed with a non-Error throw. */
  batchFailed: 'Batch execution failed.',
} as const;

/** The fallback shown when a message cannot be attributed to a known author. */
export const GENERIC_CELL_ERROR =
  'An error occurred. Check the Tessallite panel for details.';

/**
 * Server-authored 422 details the add-in surfaces VERBATIM.
 *
 * These are not the add-in's to write: the query-router authors them for a
 * user-caused, user-fixable condition, and they name only what the caller
 * already supplied (a measure name, a model slug). Everything else a 422 can
 * carry — a binder message about physical columns, join graphs or owner
 * tables — stays behind `GENERIC_CELL_ERROR`, because this runtime has no way
 * to judge a message it has never seen.
 *
 * Prefixes, because each detail interpolates the caller's own identifiers.
 */
export const SERVER_AUTHORED_DETAIL_PREFIXES: readonly string[] = [
  // Bug-9759: a date/time-variant measure (e.g. YoY growth) needs a time
  // dimension in the grain. Expected behaviour for a bare TESSALLITE.VALUE()
  // with no date context, not a server fault.
  'Period-aware time variant requires a time dimension',
  // Bug-9910: the model does not expose a measure by that name. Live shape:
  // `Unknown column: 'transaction_count' in model 'modely'`. Without this the
  // cell was a bare #VALUE! whose tooltip named neither the measure nor the
  // reason, which is why the defect was measured on ALEX as "one measure works
  // and another does not" with nothing to go on.
  'Unknown column',
];

/** True when `message` is one the add-in itself authored. */
export function isAuthoredCellMessage(message: string): boolean {
  return (Object.values(CELL_MESSAGES) as string[]).includes(message);
}

/**
 * True when `message` may be shown in a cell.
 *
 * Authored messages match EXACTLY — an exact match cannot drift the way a
 * prefix can. Server details match by prefix, because they interpolate.
 */
export function isSafeCellMessage(message: string): boolean {
  if (isAuthoredCellMessage(message)) return true;
  return SERVER_AUTHORED_DETAIL_PREFIXES.some(p => message.startsWith(p));
}
