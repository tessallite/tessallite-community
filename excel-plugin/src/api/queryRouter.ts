/**
 * Query router API.
 * Phase 2: Used by Report Builder to execute, explain, and validate queries.
 * Phase 3: Used for drill-through and member discovery.
 */
import { apiClient } from './client';
import type {
  SemanticQuery, ExecuteResponse,
  DiscoverMembersResponse, DrillOption, DrillThroughResponse,
} from '../types/tessallite';

export interface PluginExecuteParams {
  projectId: string;
  modelId: string;
  personaId?: string;
}

export async function executeQuery(
  query: SemanticQuery,
  params: PluginExecuteParams,
): Promise<ExecuteResponse> {
  return apiClient.post<ExecuteResponse>('/api/v1/plugin/execute', {
    project_id: params.projectId,
    model_id: params.modelId,
    measures: query.measures,
    dimensions: query.dimensions,
    filters: query.filters?.map(f => ({
      dimension: f.dimension,
      operator: f.operator,
      values: f.values,
    })),
    limit: query.limit,
    offset: query.offset,
    // F-025-27: the backend accepts order_by ([{field, direction}]) and offset
    // with injection-hardened validation, but the plugin never sent ordering.
    // Translate the SemanticQuery `order` map into the wire shape so the Report
    // Builder's sort selection reaches the query-router.
    order_by: query.order
      ? Object.entries(query.order).map(([field, direction]) => ({ field, direction }))
      : undefined,
    persona_id: params.personaId,
  });
}

// F-025-20 / F-025-21: the dead `explainQuery` client was removed. It posted
// the semantic JSON as `raw_query` to `/api/v1/explain`, which parses SQL — the
// plugin never builds SQL, so the call could only ever fail, and it was
// imported nowhere. Route visibility for the plugin now comes from the
// `route` field on the /plugin/execute response (see PluginRouteTrace), which
// the Query Trace modal renders.

export async function discoverMembers(
  modelId: string,
  dimensionName: string,
  personaId?: string,
): Promise<DiscoverMembersResponse> {
  return apiClient.post<DiscoverMembersResponse>('/api/v1/discover/members', {
    model_id: modelId,
    dimension_name: dimensionName,
    persona_id: personaId,
  });
}

export async function getDrillOptions(measureId: string, context: Record<string, unknown>): Promise<DrillOption[]> {
  const resp = await apiClient.post<{ hierarchies: DrillOption[] }>(`/api/v1/measures/${measureId}/drill-options`, context);
  return resp.hierarchies ?? [];
}

export async function drillThrough(
  measureId: string,
  context: Record<string, unknown>,
  cursor?: string,
): Promise<DrillThroughResponse> {
  return apiClient.post<DrillThroughResponse>(`/api/v1/measures/${measureId}/drill-through`, {
    ...context,
    cursor,
  });
}
