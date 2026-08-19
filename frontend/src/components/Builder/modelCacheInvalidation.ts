import type { QueryClient } from "@tanstack/react-query";

/**
 * The full set of model-scoped React Query keys that a hard rewrite of live
 * model state (revert, or a draft discard — G-013-01) must invalidate so every
 * builder panel re-fetches the restored definition instead of showing stale
 * pre-rewrite data.
 *
 * This is the single source of truth for that list: both the Versions dialog
 * revert handler and the pending-change discard actions call
 * {@link invalidateModelScopedCaches} so the two paths can never drift (a
 * revert that refreshed a cache a discard forgot would leave a stale panel).
 * Each key is invalidated as ``[key, projectId, modelId]``; keys stored under a
 * longer suffix (e.g. translations keyed by locale) are matched by prefix.
 */
export const MODEL_SCOPED_CACHE_KEYS: readonly string[] = [
  "versions",
  "models",
  "sources",
  "dimensions",
  "measures",
  "joins",
  "hierarchies",
  "aggregates",
  "allModelTables",
  "modelTables",
  "tableAttributes",
  "personas",
  "pockets",
  "glossary",
  // Bug-5656: snapshot-restored governance categories.
  "row-security",
  "data-tags",
  "namedSets",
  "kpis",
  "parameters",
  "data-quality-rules",
  "targets",
  "lineage",
  "aiSchedulerConfig",
  "scratchpad-measures",
  "savedQueries",
  "version-diff",
  // Bug-7152: schema-v3 caches under independent keys (translations keyed by
  // [.., modelId, locale] — the [.., modelId] prefix clears every locale).
  "alias-map",
  "model-settings",
  "translations",
  // G-013-01: the pending-change surface itself must recompute after a rewrite.
  "pending-changes",
];

export function invalidateModelScopedCaches(
  qc: QueryClient,
  projectId: string,
  modelId: string,
): void {
  for (const key of MODEL_SCOPED_CACHE_KEYS) {
    qc.invalidateQueries({ queryKey: [key, projectId, modelId] });
  }
}
