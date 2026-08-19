/**
 * Model service API.
 */
import { apiClient } from './client';
import type {
  Project, Model, Measure, Dimension, Hierarchy, HierarchyLevel, Kpi, KpiEvaluateResponse,
  KpiBatchResult, KpiBatchResponse,
  NamedSet, NamedSetPreviewResponse,
  Persona, GlossaryEntry, AliasMapEntry, DrillThroughSet, FieldCompatibilityResponse,
} from '../types/tessallite';

export async function getProjects(): Promise<Project[]> {
  const raw = await apiClient.get<Record<string, unknown>[]>('/api/v1/projects');
  return raw.map(p => ({
    id: String(p.id),
    name: String(p.display_name ?? p.name ?? p.slug ?? ''),
    slug: String(p.slug ?? ''),
  }));
}

export async function getModels(projectId: string): Promise<Model[]> {
  const raw = await apiClient.get<Record<string, unknown>[]>(`/api/v1/projects/${projectId}/models`);
  return raw.map(m => ({
    id: String(m.id),
    name: String(m.display_name ?? m.name ?? m.slug ?? ''),
    slug: String(m.slug ?? ''),
    description: m.description != null ? String(m.description) : undefined,
    deployed_version_id: m.deployed_version_id != null ? String(m.deployed_version_id) : undefined,
    deployed: Boolean(m.deployed),
  }));
}

export async function getModel(projectId: string, modelId: string): Promise<Model> {
  const m = await apiClient.get<Record<string, unknown>>(`/api/v1/projects/${projectId}/models/${modelId}`);
  return {
    id: String(m.id),
    name: String(m.display_name ?? m.name ?? m.slug ?? ''),
    slug: String(m.slug ?? ''),
    description: m.description != null ? String(m.description) : undefined,
    deployed_version_id: m.deployed_version_id != null ? String(m.deployed_version_id) : undefined,
    deployed: Boolean(m.deployed),
  };
}

export async function getMeasures(projectId: string, modelId: string, personaId?: string): Promise<Measure[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  return apiClient.get<Measure[]>(`/api/v1/projects/${projectId}/models/${modelId}/measures${params}`);
}

export async function getMeasureDetail(
  projectId: string, modelId: string, measureId: string,
): Promise<Measure> {
  return apiClient.get<Measure>(
    `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}`,
  );
}

export async function getDimensions(projectId: string, modelId: string, personaId?: string): Promise<Dimension[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  return apiClient.get<Dimension[]>(`/api/v1/projects/${projectId}/models/${modelId}/dimensions${params}`);
}

export async function getHierarchies(projectId: string, modelId: string, personaId?: string): Promise<Hierarchy[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  return apiClient.get<Hierarchy[]>(`/api/v1/projects/${projectId}/models/${modelId}/hierarchies${params}`);
}

/**
 * Fetch a single hierarchy's full level detail.
 *
 * The list endpoint returns only `level_names`; the detail endpoint returns
 * each level's key-attribute (the technical dimension the level maps to). The
 * Report Builder needs this to translate a dropped hierarchy level into its
 * bindable dimension (F-025-11). Maps the backend `levels[].key_attribute.name`
 * onto the plugin's `HierarchyLevel.dimensionName`.
 */
export async function getHierarchyDetail(
  projectId: string, modelId: string, hierarchyId: string, personaId?: string,
): Promise<HierarchyLevel[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  const raw = await apiClient.get<{
    levels?: { name: string; ordinal: number; time_unit?: string | null; key_attribute?: { name?: string } | null }[];
  }>(`/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}${params}`);
  return (raw.levels ?? []).map(lv => ({
    name: lv.name,
    level_number: lv.ordinal,
    time_unit: lv.time_unit ?? undefined,
    dimensionName: lv.key_attribute?.name,
  }));
}

/**
 * Query string for a CONSUMPTION-surface read (Bug-8710).
 *
 * The Excel add-in is an end-user BI transport, not an authoring surface. The
 * deployed model snapshot is the single serving authority for a definition:
 * "the deployed snapshot is the contract; the live state is editor-only"
 * (F-013-01, architecture_model-versioning-and-deploy.md), stated operationally
 * for these routes by F-017-05 — BI catalogue surfaces send `deployed_only`,
 * and only the model builder omits it.
 *
 * Without it the pane advertised definitions nobody had deployed, and the CUBE
 * formula it then inserted was resolved by the GATEWAY, which serves the
 * deployed snapshot — so the draft the pane showed produced a permanent #N/A.
 * A modeller who wants to see drafts uses the model builder.
 */
function consumptionQuery(personaId?: string): string {
  const params = new URLSearchParams({ deployed_only: 'true' });
  if (personaId) params.set('persona_id', personaId);
  return `?${params.toString()}`;
}

export async function getKpis(projectId: string, modelId: string, personaId?: string): Promise<Kpi[]> {
  return apiClient.get<Kpi[]>(
    `/api/v1/projects/${projectId}/models/${modelId}/kpis${consumptionQuery(personaId)}`,
  );
}

export async function evaluateKpi(projectId: string, modelId: string, kpiId: string, personaId?: string): Promise<KpiEvaluateResponse> {
  // Bug-6361: thread the active persona so the single-KPI evaluation (Report
  // Builder KPI cards) returns the same value the batch path and the rest of the
  // pane show, instead of the default-persona value (cross-surface mismatch).
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  return apiClient.post<KpiEvaluateResponse>(`/api/v1/projects/${projectId}/models/${modelId}/kpis/${kpiId}/evaluate${params}`);
}

/**
 * Evaluate a batch of KPIs.
 *
 * F-025-03: the endpoint requires a `{kpi_ids: [...]}` body (a body-less POST
 * 422s) and returns the canonical `KPIBatchResponse` envelope
 * `{ results: KPIEvaluateResponse[], evaluation_ms }` — not a bare array.
 * The caller passes the loaded KPI ids and receives the unwrapped per-KPI
 * results. `persona_id` is threaded so KPI values match the active persona
 * (consistent with the Report Builder).
 */
export async function evaluateKpiBatch(
  projectId: string,
  modelId: string,
  kpiIds: string[],
  personaId?: string,
): Promise<KpiBatchResult[]> {
  if (kpiIds.length === 0) return [];
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  const resp = await apiClient.post<KpiBatchResponse>(
    `/api/v1/projects/${projectId}/models/${modelId}/kpis/evaluate-batch${params}`,
    { kpi_ids: kpiIds },
  );
  return resp.results ?? [];
}

export async function getNamedSets(projectId: string, modelId: string, personaId?: string): Promise<NamedSet[]> {
  return apiClient.get<NamedSet[]>(
    `/api/v1/projects/${projectId}/models/${modelId}/named-sets${consumptionQuery(personaId)}`,
  );
}

/**
 * Bug-8712: preview returns the MEMBERS that are written into worksheet cells,
 * so an unpinned read changes the numbers in someone's spreadsheet the moment a
 * modeller edits an expression — no Deploy required. `deployed_only` on this
 * route means "compute from the deployed definition", not merely "filter": the
 * flag had to be added to the endpoint, because the list route's flag resolves
 * a definition and this route builds its query from the row's own
 * builder_definition/expression.
 */
export async function previewNamedSet(projectId: string, modelId: string, setId: string, personaId?: string): Promise<NamedSetPreviewResponse> {
  return apiClient.post<NamedSetPreviewResponse>(
    `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${setId}/preview${consumptionQuery(personaId)}`,
  );
}

export async function getPersonas(projectId: string, modelId: string): Promise<Persona[]> {
  return apiClient.get<Persona[]>(`/api/v1/projects/${projectId}/models/${modelId}/personas`);
}

export async function getGlossary(projectId: string, modelId: string, personaId?: string): Promise<GlossaryEntry[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  return apiClient.get<GlossaryEntry[]>(`/api/v1/projects/${projectId}/models/${modelId}/glossary${params}`);
}

export async function getAliasMap(projectId: string, modelId: string, personaId?: string): Promise<AliasMapEntry[]> {
  const params = personaId ? `?persona_id=${encodeURIComponent(personaId)}` : '';
  const resp = await apiClient.get<{ model_id: string; alias_map: Record<string, string> }>(
    `/api/v1/projects/${projectId}/models/${modelId}/alias-map${params}`,
  );
  const map = resp.alias_map ?? {};
  return Object.entries(map).map(([alias, canonical]) => ({
    alias,
    canonical,
    object_type: 'measure' as const,
  }));
}

export async function getFieldCompatibility(
  projectId: string,
  modelId: string,
  options: {
    personaId?: string | null;
    measureIds?: string[];
    dimensionIds?: string[];
  } = {},
): Promise<FieldCompatibilityResponse> {
  const params = new URLSearchParams();
  if (options.personaId) params.set('persona_id', options.personaId);
  for (const id of [...new Set(options.measureIds ?? [])].sort()) {
    if (id) params.append('measure_ids', id);
  }
  for (const id of [...new Set(options.dimensionIds ?? [])].sort()) {
    if (id) params.append('dimension_ids', id);
  }
  const query = params.toString();
  return apiClient.get<FieldCompatibilityResponse>(
    `/api/v1/projects/${projectId}/models/${modelId}/field-compatibility${query ? `?${query}` : ''}`,
  );
}

export async function getDrillThroughSet(
  projectId: string, modelId: string, measureId: string,
): Promise<DrillThroughSet | null> {
  try {
    return await apiClient.get<DrillThroughSet>(
      `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}/drill-through-set/enriched`,
    );
  } catch {
    return null;
  }
}

export interface EntityUsageCreate {
  workbook_id?: string;
  worksheet?: string;
  cell_reference?: string;
  usage_type: string;
}

export async function reportNamedSetUsage(
  projectId: string, modelId: string, namedSetId: string, body: EntityUsageCreate,
): Promise<void> {
  await apiClient.post(`/api/v1/projects/${projectId}/models/${modelId}/named-sets/${namedSetId}/usage`, body);
}

export async function reportKpiUsage(
  projectId: string, modelId: string, kpiId: string, body: EntityUsageCreate,
): Promise<void> {
  await apiClient.post(`/api/v1/projects/${projectId}/models/${modelId}/kpis/${kpiId}/usage`, body);
}
