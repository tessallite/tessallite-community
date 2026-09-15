/**
 * What the canvas does with a failed layout operation.
 *
 * One authority, because the cost of the two mistakes is asymmetric and each
 * has already shipped once:
 *
 *   Discarding on an engine fault  — the user's drag is undone. Annoying, but
 *                                    visible, explained, and they can redo it.
 *   Keeping on a geometry fault    — geometry nothing could validate is saved
 *                                    silently. The user finds out when they
 *                                    reopen the model and the diagram is wrong.
 *
 * So the rule is: keep the edit only when the engine demonstrably never formed
 * an opinion about it. Anything else discards.
 */
import type { LayoutFailureCode } from "./types";

export type FailureDisposition =
  /** The engine judged this geometry undrawable. Restore the previous layout. */
  | "discard"
  /** The engine never answered. No evidence against the edit, so keep it. */
  | "keep"
  /** A newer gesture or model owns the canvas now. Touch nothing at all. */
  | "abandon";

export function dispositionFor(code: LayoutFailureCode): FailureDisposition {
  switch (code) {
    case "geometry-invalid":
    case "no-route":
    // The canvas built the request, so malformed input is a defect here — but
    // the candidate still went unvalidated, and unvalidated geometry is not
    // something to persist.
    case "invalid-input":
      return "discard";

    case "engine-unavailable":
    case "timeout":
      return "keep";

    case "cancelled":
    case "superseded":
      return "abandon";

    // Every rejection origin in the layout path now throws a typed LayoutError,
    // so `unknown` means an unexpected fault rather than a known-safe one.
    // Treating it as safe-to-keep is exactly the defect this policy exists to
    // prevent: a real geometry rejection that arrived unclassified was kept,
    // flushed and recorded as if it had succeeded.
    case "unknown":
    default:
      return "discard";
  }
}
