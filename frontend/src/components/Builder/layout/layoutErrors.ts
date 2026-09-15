/**
 * Typed layout failures.
 *
 * The canvas decides whether to KEEP or DISCARD the user's edit from the
 * failure code alone, so the code has to be established where the rejection is
 * established — not guessed later from an English message.
 *
 * Before this module every rejection threw a plain `Error`. The worker
 * classified only errors that already carried a `.code`, so a real geometry
 * rejection ("relationship is not orthogonal: j") arrived as `unknown`, and
 * `unknown` is the branch that KEEPS the candidate and saves it. The engine
 * could therefore detect an incoherent route and then persist the very edit
 * that caused it.
 *
 * The distinctions that matter to that decision:
 *
 *   invalid-input      the request was malformed. The canvas built it, so this
 *                      is a defect, not the user's edit.
 *   geometry-invalid   the edit produces geometry that cannot be drawn. Discard
 *                      it and restore the previous layout.
 *   no-route           no path exists between two cards. Same treatment as
 *                      geometry-invalid, kept separate because the remedy the
 *                      user is offered differs.
 *   engine-unavailable the router never rendered a verdict. There is no
 *                      evidence against the user's edit, so KEEP it.
 *
 * Deliberately no module-level dependency on the worker, React or the engines:
 * this is thrown from pure geometry code and read on the main thread.
 */
import type { LayoutFailureCode } from "./types";

export class LayoutError extends Error {
  readonly code: LayoutFailureCode;

  constructor(code: LayoutFailureCode, message: string) {
    super(message);
    this.name = "LayoutError";
    this.code = code;
  }
}

/** The request the canvas handed the engine is malformed. */
export function invalidInput(message: string): LayoutError {
  return new LayoutError("invalid-input", message);
}

/** The resulting geometry cannot be drawn. The edit must not be kept. */
export function geometryInvalid(message: string): LayoutError {
  return new LayoutError("geometry-invalid", message);
}

/** No path exists between the two cards. */
export function noRoute(message: string): LayoutError {
  return new LayoutError("no-route", message);
}

/** The routing engine did not answer. Says nothing about the user's edit. */
export function engineUnavailable(message: string): LayoutError {
  return new LayoutError("engine-unavailable", message);
}

/**
 * The code carried by a thrown value, or `unknown` when it carries none.
 *
 * `unknown` must keep meaning "nobody classified this", never "safe to keep" —
 * the caller decides what to do with an unclassified failure, and the safe
 * reading is that the engine failed rather than that the edit was bad.
 */
export function failureCodeOf(error: unknown): LayoutFailureCode {
  if (error instanceof LayoutError) return error.code;
  // Structured clone across the worker boundary drops the prototype, so a
  // LayoutError that has already crossed it arrives as a plain object.
  if (error && typeof error === "object" && "code" in error) {
    const code = String((error as { code?: unknown }).code);
    if (
      code === "invalid-input" || code === "engine-unavailable" || code === "no-route" ||
      code === "geometry-invalid" || code === "cancelled" || code === "timeout"
    ) {
      return code;
    }
  }
  return "unknown";
}
