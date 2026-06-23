import { useQuery } from "@tanstack/react-query";
import api from "./client";

export type VersionItem = {
  id: string;
  version_number: number;
  summary: string | null;
  created_at: string;
  created_by: string;
  is_deployed: boolean;
};

export type DeployResponse = {
  status: "ok";
  deployed_version_id: string;
  last_deployed_at: string;
};

const base = (projectId: string, modelId: string) =>
  `/api/v1/projects/${encodeURIComponent(projectId)}/models/${encodeURIComponent(modelId)}`;

export type VersionDiffCategory = {
  added: Record<string, unknown>[];
  removed: Record<string, unknown>[];
  changed: Array<{ id: string; changes: Record<string, { from: unknown; to: unknown }> }>;
};

export type VersionDiff = {
  version_a: number;
  version_b: number;
  diff: Record<string, VersionDiffCategory>;
};

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
      .post(
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
