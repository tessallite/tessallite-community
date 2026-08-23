import { useQuery } from "@tanstack/react-query";
import api from "./client";

export type VersionItem = {
  id: string;
  version_number: number;
  summary: string | null;
  created_at: string;
  created_by: string;
  is_deployed: boolean;
  // Bug-6295: true for imported history rows whose original shape the backup
  // bundle did not carry. Such a version cannot be reverted to (the backend
  // returns 409); the UI must not offer Revert for it.
  snapshot_unavailable?: boolean;
};

export type DeployResponse = {
  status: "ok";
  deployed_version_id: string;
  last_deployed_at: string;
};

/**
 * The only structured refusal produced by the model deploy route.  Keeping the
 * finite error domain here prevents each deploy surface from making its own
 * unsafe assumption about Axios' `detail` value.
 */
export type JoinPopulationBlockedOffender = {
  join_id: string;
  join_label?: string | null;
  left_table_name?: string | null;
  right_table_name?: string | null;
  left_column_name?: string | null;
  right_column_name?: string | null;
  population_participation: string;
  status: string;
  row_effect_ratio: number | null;
  reason: string | null;
};

export type JoinPopulationBlockedDetail = {
  code: "JOIN_POPULATION_BLOCKED";
  message: string;
  threshold: number;
  joins: JoinPopulationBlockedOffender[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** Parse the backend's typed 409 without ever returning a partial contract. */
export function parseJoinPopulationBlockedError(
  error: unknown,
): JoinPopulationBlockedDetail | null {
  const response = isRecord(error) ? error.response : undefined;
  const responseData = isRecord(response) ? response.data : undefined;
  const detail = isRecord(responseData) ? responseData.detail : undefined;
  if (
    !isRecord(detail) ||
    detail.code !== "JOIN_POPULATION_BLOCKED" ||
    typeof detail.message !== "string" ||
    typeof detail.threshold !== "number" ||
    !Number.isFinite(detail.threshold) ||
    !Array.isArray(detail.joins)
  ) {
    return null;
  }

  const joins: JoinPopulationBlockedOffender[] = [];
  for (const raw of detail.joins) {
    if (!isRecord(raw) || typeof raw.join_id !== "string") return null;
    if (
      typeof raw.population_participation !== "string" ||
      typeof raw.status !== "string" ||
      (raw.row_effect_ratio !== null &&
        typeof raw.row_effect_ratio !== "number") ||
      (raw.reason !== null && typeof raw.reason !== "string")
    ) {
      return null;
    }
    joins.push({
      join_id: raw.join_id,
      join_label: typeof raw.join_label === "string" ? raw.join_label : null,
      left_table_name:
        typeof raw.left_table_name === "string" ? raw.left_table_name : null,
      right_table_name:
        typeof raw.right_table_name === "string" ? raw.right_table_name : null,
      left_column_name:
        typeof raw.left_column_name === "string" ? raw.left_column_name : null,
      right_column_name:
        typeof raw.right_column_name === "string" ? raw.right_column_name : null,
      population_participation: raw.population_participation,
      status: raw.status,
      row_effect_ratio: raw.row_effect_ratio,
      reason: raw.reason,
    });
  }
  return {
    code: "JOIN_POPULATION_BLOCKED",
    message: detail.message,
    threshold: detail.threshold,
    joins,
  };
}

export type GitCommitEntry = {
  sha: string;
  type: string;
  message: string;
  version: number | null;
  tags: string[];
  author: string;
  timestamp: string;
};

/** Bug-7142: revert response now surfaces preserved-governance state. */
export type RevertResponse = {
  status: "ok";
  reverted_to: string;
  governance_preserved: boolean;
  governance_preserved_note: string;
};

const base = (projectId: string, modelId: string) =>
  `/api/v1/projects/${encodeURIComponent(projectId)}/models/${encodeURIComponent(modelId)}`;

export type VersionDiffCategory = {
  added: Record<string, unknown>[];
  removed: Record<string, unknown>[];
  changed: Array<{ id: string; changes: Record<string, { from: unknown; to: unknown }> }>;
};

// Bug-5916: singleton/dict snapshot keys (model, model_alias_map,
// refresh_sla_config, ai_scheduler_config, model_settings) diff to a
// field-level "changes" map instead of an added/removed/changed row list.
export type VersionDiffSingleton = {
  changes: Record<string, { from: unknown; to: unknown }>;
};

export function isSingletonDiff(
  entry: VersionDiffCategory | VersionDiffSingleton,
): entry is VersionDiffSingleton {
  return !("added" in entry);
}

export type VersionDiff = {
  version_a: number;
  version_b: number;
  diff: Record<string, VersionDiffCategory | VersionDiffSingleton>;
};

/** Shared shape of a per-category diff payload (matches the backend
 *  ``diff_snapshots`` output), reused by version diff and pending changes. */
export type DiffMap = Record<string, VersionDiffCategory | VersionDiffSingleton>;

// G-013-01 (Bug-9171): the two pending change sets the Model Builder surfaces.
// Field names are aligned end-to-end with the model-service
// PendingChangesResponse / PendingUnsaved / PendingSavedUndeployed schema.
export type PendingUnsaved = {
  base_version: number | null; // latest saved version_number (null = never saved)
  base_version_unavailable: boolean; // last saved version is an imported placeholder
  diff: DiffMap; // latest saved -> live/draft
};

export type PendingSavedUndeployed = {
  deployed_version: number | null; // currently-serving version_number (null = none)
  saved_version: number | null; // latest saved version_number (null = never saved)
  diff: DiffMap; // deployed -> latest saved
};

export type PendingChanges = {
  unsaved: PendingUnsaved;
  saved_undeployed: PendingSavedUndeployed;
};

export type DiscardDraftResponse = {
  status: string;
  restored_to_version: number;
};

/** Count the added/removed/changed rows (or singleton field changes) in a diff
 *  map — the same reduction the diff panel uses for its change chip. */
export function countDiffChanges(diff: DiffMap): number {
  return Object.values(diff).reduce(
    (n, cat) =>
      n +
      (isSingletonDiff(cat)
        ? Object.keys(cat.changes).length
        : cat.added.length + cat.removed.length + cat.changed.length),
    0,
  );
}

export const versionsApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<{ items: VersionItem[] }>(`${base(projectId, modelId)}/versions`)
      .then((r) => r.data.items),
  create: (projectId: string, modelId: string, summary?: string) =>
    api
      .post<VersionItem>(`${base(projectId, modelId)}/versions`, { summary })
      .then((r) => r.data),
  get: (projectId: string, modelId: string, versionId: string) =>
    api
      .get(`${base(projectId, modelId)}/versions/${encodeURIComponent(versionId)}`)
      .then((r) => r.data),
  revert: (projectId: string, modelId: string, versionId: string, confirmText: string) =>
    api
      .post<RevertResponse>(
        `${base(projectId, modelId)}/versions/${encodeURIComponent(versionId)}/revert`,
        { confirm: confirmText },
      )
      .then((r) => r.data),
  deploy: (projectId: string, modelId: string, versionId?: string) =>
    api
      .post<DeployResponse>(`${base(projectId, modelId)}/deploy`, {
        version_id: versionId,
      })
      .then((r) => r.data),
  undeploy: (projectId: string, modelId: string) =>
    api
      .post(`${base(projectId, modelId)}/undeploy`)
      .then((r) => r.data),
  diff: (projectId: string, modelId: string, vaId: string, vbId: string) =>
    api
      .get<VersionDiff>(
        `${base(projectId, modelId)}/versions/${encodeURIComponent(vaId)}/diff/${encodeURIComponent(vbId)}`,
      )
      .then((r) => r.data),
  gitLog: (projectId: string, modelId: string, limit = 50, offset = 0) =>
    api
      .get<{ commits: GitCommitEntry[] }>(
        `${base(projectId, modelId)}/git/log`,
        { params: { limit, offset } },
      )
      .then((r) => r.data.commits),
  gitDiff: (projectId: string, modelId: string, sha1: string, sha2: string) =>
    api
      .get<{ diff_text: string }>(
        `${base(projectId, modelId)}/git/diff/${encodeURIComponent(sha1)}/${encodeURIComponent(sha2)}`,
      )
      .then((r) => r.data.diff_text),
  // G-013-01 (Bug-9171): pending-change review + draft discard.
  pendingChanges: (projectId: string, modelId: string) =>
    api
      .get<PendingChanges>(`${base(projectId, modelId)}/pending-changes`)
      .then((r) => r.data),
  discardDraft: (projectId: string, modelId: string) =>
    api
      .post<DiscardDraftResponse>(`${base(projectId, modelId)}/discard-draft`)
      .then((r) => r.data),
};

export function useVersionDiff(
  projectId: string | undefined,
  modelId: string | undefined,
  vaId: string | undefined,
  vbId: string | undefined,
) {
  return useQuery({
    queryKey: ["version-diff", projectId, modelId, vaId, vbId],
    queryFn: () => versionsApi.diff(projectId!, modelId!, vaId!, vbId!),
    enabled: !!(projectId && modelId && vaId && vbId),
  });
}

export function usePendingChanges(
  projectId: string | undefined,
  modelId: string | undefined,
  enabled = true,
) {
  return useQuery({
    queryKey: ["pending-changes", projectId, modelId],
    queryFn: () => versionsApi.pendingChanges(projectId!, modelId!),
    enabled: !!(projectId && modelId) && enabled,
  });
}
