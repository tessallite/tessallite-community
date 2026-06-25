import api from "./client";

export type ConnectionStub = {
  id: string;
  role: "source" | "target";
  display_name: string | null;
  connection_type: string | null;
};

export type ExportBundle = {
  schema_version: number;
  export_format: string;
  exported_at: string;
  exported_from: Record<string, string>;
  model_display_name: string;
  model_slug: string;
  snapshot: Record<string, unknown>;
};

export type ExportPreviewResponse = {
  bundle: ExportBundle;
  connections_required: ConnectionStub[];
};

export type ImportRequest = {
  bundle: ExportBundle;
  target_project_id: string;
  target_slug?: string | null;
  target_display_name?: string | null;
  connection_mapping: Record<string, string>;
  deploy_immediately?: boolean;
};

export type ImportResponse = {
  model_id: string;
  slug: string;
  display_name: string;
  deployed_version_id: string | null;
  missing_connections: string[];
};

const modelBase = (projectId: string, modelId: string) =>
  `/api/v1/projects/${encodeURIComponent(projectId)}/models/${encodeURIComponent(modelId)}`;

const projectBase = (projectId: string) =>
  `/api/v1/projects/${encodeURIComponent(projectId)}/models`;

export const importExportApi = {
  exportModel: (projectId: string, modelId: string) =>
    api
      .get<ExportPreviewResponse>(`${modelBase(projectId, modelId)}/snapshot-export`)
      .then((r) => r.data),
  importModel: (projectId: string, body: ImportRequest) =>
    api
      .post<ImportResponse>(`${projectBase(projectId)}/snapshot-import`, body)
      .then((r) => r.data),
};

export const lookmlExportApi = {
  exportModel: (projectId: string, modelId: string, connection: string) =>
    api
      .post(
        `${modelBase(projectId, modelId)}/export/lookml`,
        { connection },
        { responseType: "blob" },
      )
      .then((r) => r.data as Blob),
};

// --- Project-level import/export ---

export type ProjectExportRequest = {
  include_credentials: boolean;
  passphrase?: string | null;
  sections: string[];
};

export type ProjectBundle = {
  schema_version: number;
  export_format: string;
  exported_at: string;
  exported_from: Record<string, string>;
  credentials_included: boolean;
  credentials_envelope: Record<string, unknown> | null;
  included_sections: string[];
  project: {
    slug: string;
    display_name: string;
    is_active: boolean;
  };
  connections?: Array<{
    id: string;
    display_name: string;
    connection_type: string;
    config: Record<string, unknown>;
    credentials?: string;
  }>;
  llm_configs?: Array<Record<string, unknown>>;
  agent_config?: Record<string, unknown> | null;
  cross_model_recipes?: Array<Record<string, unknown>>;
  project_settings?: Array<{ key: string; value: unknown }>;
  access_bindings?: Array<{
    user_identity: string;
    role: string;
    model_slug: string | null;
  }>;
  models: Array<Record<string, unknown>>;
  test_metadata?: Record<string, unknown> | null;
};

export type ProjectImportRequest = {
  bundle: ProjectBundle;
  passphrase?: string | null;
  mode: "create" | "replace";
  dry_run?: boolean;
  project_slug?: string | null;
  project_display_name?: string | null;
  model_slugs?: Record<string, string> | null;
  persona_slugs?: Record<string, string> | null;
  connection_mapping?: Record<string, string> | null;
  override_connections?: boolean;
};

export type ProjectImportConnectionAction = {
  export_connection_id: string;
  display_name: string | null;
  connection_type: string | null;
  action: string;
  target_connection_id: string | null;
};

export type ProjectImportModelCascadeCounts = {
  aggregates: number;
  pockets: number;
  query_logs: number;
  query_miss_logs: number;
  route_logs: number;
};

export type ProjectImportModelCascade = {
  model_id: string;
  slug: string;
  display_name: string;
  counts: ProjectImportModelCascadeCounts;
};

export type ProjectImportCascadeCounts = {
  per_model: ProjectImportModelCascade[];
  totals: Partial<ProjectImportModelCascadeCounts>;
};

export type ProjectImportPlan = {
  mode: string;
  target_project_id: string | null;
  target_project_slug: string;
  target_project_display_name: string;
  target_project_exists: boolean;
  will_create_project: boolean;
  will_replace_project: boolean;
  delete_counts: Record<string, number>;
  model_cascade_counts: ProjectImportCascadeCounts;
  incoming_counts: Record<string, number>;
  connection_actions: ProjectImportConnectionAction[];
  model_slugs: string[];
  post_import_actions: string[];
  warnings: string[];
};

export type ProjectImportResponse = {
  project_id: string | null;
  project_slug: string;
  id_map: Record<string, Record<string, string>>;
  models_imported: number;
  models_requiring_deploy: string[];
  post_import_actions: string[];
  warnings: string[];
  dry_run?: boolean;
  plan?: ProjectImportPlan | null;
};

export const projectImportExportApi = {
  exportProject: (projectId: string, body: ProjectExportRequest) =>
    api
      .post<ProjectBundle>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/export`,
        body,
      )
      .then((r) => r.data),
  importProject: (body: ProjectImportRequest) =>
    api
      .post<ProjectImportResponse>("/api/v1/projects/import", body)
      .then((r) => r.data),
};

// --- YAML export/import ---

export type YamlImportResponse = {
  models_parsed: number;
  models_created: number;
  warnings: string[];
  model_names: string[];
};

export type DbtImportResponse = {
  models_parsed: number;
  models_created: number;
  model_names: string[];
  warnings: string[];
  bundle: Record<string, unknown>;
};

export type CubeImportResponse = {
  models_parsed: number;
  models_created: number;
  model_names: string[];
  warnings: string[];
  bundle: Record<string, unknown>;
};

export type AtScaleImportResponse = {
  models_parsed: number;
  models_created: number;
  model_names: string[];
  warnings: string[];
  bundle: Record<string, unknown>;
};

export type CatalogType = "datahub" | "openmetadata" | "alation";

export type CatalogImportRequest = {
  catalog_type: CatalogType;
  api_url: string;
  api_token: string;
  dataset_filter?: string;
  model_name?: string;
};

export type CatalogImportResponse = {
  models_created: number;
  model_names: string[];
  tables_imported: number;
  dimensions_imported: number;
  measures_imported: number;
  warnings: string[];
};

export const dbtImportApi = {
  importDbt: (projectId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api
      .post<DbtImportResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/import/dbt`,
        form,
        { headers: { "Content-Type": "multipart/form-data" } },
      )
      .then((r) => r.data);
  },
};

export const cubeImportApi = {
  importCube: (projectId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api
      .post<CubeImportResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/import/cube`,
        form,
        { headers: { "Content-Type": "multipart/form-data" } },
      )
      .then((r) => r.data);
  },
};

export const atscaleImportApi = {
  importAtScale: (projectId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api
      .post<AtScaleImportResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/import/atscale`,
        form,
        { headers: { "Content-Type": "multipart/form-data" } },
      )
      .then((r) => r.data);
  },
};

export const catalogImportApi = {
  importCatalog: (projectId: string, body: CatalogImportRequest) =>
    api
      .post<CatalogImportResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/import/catalog`,
        body,
      )
      .then((r) => r.data),
};

export const yamlExportApi = {
  exportProject: (projectId: string) =>
    api
      .post(
        `/api/v1/projects/${encodeURIComponent(projectId)}/export/yaml`,
        {},
        { responseType: "blob" },
      )
      .then((r) => r.data as Blob),
  importProject: (projectId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api
      .post<YamlImportResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/import/yaml`,
        form,
        { headers: { "Content-Type": "multipart/form-data" } },
      )
      .then((r) => r.data);
  },
};
