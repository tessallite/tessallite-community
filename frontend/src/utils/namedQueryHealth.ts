/**
 * Pure health derivation for a Named Query's materialised artifact.
 *
 * Mirrors the backend lifecycle: only an artifact in `fresh` serves from the
 * materialised table; every other status is a refusal signal (the resolver
 * falls back to live or errors). `invalidating` is the transient in-build
 * state, surfaced as stale so the UI never claims freshness mid-build.
 *
 * Pure and i18n-agnostic on purpose: the caller translates `reasonKey` (or
 * shows the server's own `detail` / `failure_reason` verbatim).
 */

export type NamedQueryHealthStatus = "fresh" | "stale" | "failed";

export interface NamedQueryHealth {
  status: NamedQueryHealthStatus;
  /** i18n key for a generic reason, or null when `detail` carries the reason. */
  reasonKey: string | null;
  /** Server-supplied reason text (artifact.failure_reason), if any. */
  detail: string | null;
}

export interface NamedQueryHealthSource {
  artifact: {
    status: string;
    failure_reason: string | null;
    retired_at: string | null;
  } | null;
}

/**
 * Map artifact lifecycle status to the fresh/stale/failed UI triad.
 *
 * - no artifact at all: never materialised -> stale ("never refreshed").
 * - `fresh`: serving the materialised table.
 * - `failed`: last refresh failed; the server reason is shown when present.
 * - `invalidating`: a rebuild is running -> stale ("refresh in progress").
 * - `stale` / `retired` / unknown: refusal signal -> stale.
 */
export function namedQueryHealth(nq: NamedQueryHealthSource): NamedQueryHealth {
  const artifact = nq.artifact;
  if (!artifact) {
    return { status: "stale", reasonKey: "namedQueries.healthNeverRefreshed", detail: null };
  }
  switch (artifact.status) {
    case "fresh":
      return { status: "fresh", reasonKey: null, detail: null };
    case "failed":
      return {
        status: "failed",
        reasonKey: artifact.failure_reason ? null : "namedQueries.healthFailedGeneric",
        detail: artifact.failure_reason,
      };
    case "invalidating":
      return { status: "stale", reasonKey: "namedQueries.healthRefreshing", detail: null };
    case "stale":
    default:
      return {
        status: "stale",
        reasonKey: artifact.failure_reason ? null : "namedQueries.healthStaleGeneric",
        detail: artifact.failure_reason,
      };
  }
}

/** Chip colour for a health status — MUI semantic colours only. */
export function namedQueryHealthColor(
  status: NamedQueryHealthStatus,
): "success" | "warning" | "error" {
  switch (status) {
    case "fresh":
      return "success";
    case "failed":
      return "error";
    default:
      return "warning";
  }
}
