import type { Project, Model, Measure, Dimension, Hierarchy, HierarchyLevel, Kpi, KpiEvaluateResponse, KpiBatchResult, NamedSet, NamedSetPreviewResponse, Persona, GlossaryEntry, AliasMapEntry, DrillThroughSet, FieldCompatibilityResponse } from '../types/tessallite';
export declare function getProjects(signal?: AbortSignal): Promise<Project[]>;
export declare function getModels(projectId: string, signal?: AbortSignal): Promise<Model[]>;
export declare function getModel(projectId: string, modelId: string): Promise<Model>;
export declare function getMeasures(projectId: string, modelId: string, personaId?: string): Promise<Measure[]>;
export declare function getMeasureDetail(projectId: string, modelId: string, measureId: string): Promise<Measure>;
export declare function getDimensions(projectId: string, modelId: string, personaId?: string): Promise<Dimension[]>;
export declare function getHierarchies(projectId: string, modelId: string, personaId?: string): Promise<Hierarchy[]>;
/**
 * Fetch a single hierarchy's full level detail.
 *
 * The list endpoint returns only `level_names`; the detail endpoint returns
 * each level's key-attribute (the technical dimension the level maps to). The
 * Report Builder needs this to translate a dropped hierarchy level into its
 * bindable dimension (F-025-11). Maps the backend `levels[].key_attribute.name`
 * onto the plugin's `HierarchyLevel.dimensionName`.
 */
export declare function getHierarchyDetail(projectId: string, modelId: string, hierarchyId: string, personaId?: string): Promise<HierarchyLevel[]>;
export declare function getKpis(projectId: string, modelId: string, personaId?: string): Promise<Kpi[]>;
export declare function evaluateKpi(projectId: string, modelId: string, kpiId: string, personaId?: string): Promise<KpiEvaluateResponse>;
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
export declare function evaluateKpiBatch(projectId: string, modelId: string, kpiIds: string[], personaId?: string): Promise<KpiBatchResult[]>;
export declare function getNamedSets(projectId: string, modelId: string, personaId?: string): Promise<NamedSet[]>;
/**
 * Bug-8712: preview returns the MEMBERS that are written into worksheet cells,
 * so an unpinned read changes the numbers in someone's spreadsheet the moment a
 * modeller edits an expression — no Deploy required. `deployed_only` on this
 * route means "compute from the deployed definition", not merely "filter": the
 * flag had to be added to the endpoint, because the list route's flag resolves
 * a definition and this route builds its query from the row's own
 * builder_definition/expression.
 */
export declare function previewNamedSet(projectId: string, modelId: string, setId: string, personaId?: string): Promise<NamedSetPreviewResponse>;
export declare function getPersonas(projectId: string, modelId: string): Promise<Persona[]>;
export declare function getGlossary(projectId: string, modelId: string, personaId?: string): Promise<GlossaryEntry[]>;
export declare function getAliasMap(projectId: string, modelId: string, personaId?: string): Promise<AliasMapEntry[]>;
export declare function getFieldCompatibility(projectId: string, modelId: string, options?: {
    personaId?: string | null;
    measureIds?: string[];
    dimensionIds?: string[];
}): Promise<FieldCompatibilityResponse>;
export declare function getDrillThroughSet(projectId: string, modelId: string, measureId: string): Promise<DrillThroughSet | null>;
export interface EntityUsageCreate {
    workbook_id?: string;
    worksheet?: string;
    cell_reference?: string;
    usage_type: string;
}
export declare function reportNamedSetUsage(projectId: string, modelId: string, namedSetId: string, body: EntityUsageCreate): Promise<void>;
export declare function reportKpiUsage(projectId: string, modelId: string, kpiId: string, body: EntityUsageCreate): Promise<void>;
