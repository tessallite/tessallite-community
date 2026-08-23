// Deployed query-router catalogue.  This is deliberately separate from the
// model-service authoring types: the query surface must describe the
// published serving contract, including the exact session variable key and
// why a named object cannot be consumed by SQL.

export interface DeployedParameterCatalogueItem {
  name: string;
  /** Exact persisted/deployed name (may retain a leading '@'). */
  canonical_name: string;
  param_type: string;
  display_name: string | null;
  description: string | null;
  default_value: unknown;
  allowed_values: unknown[] | null;
  has_default: boolean;
  session_var_key: string;
  /** False when the row must not be offered as a usable SQL override. */
  sql_usable: boolean;
  unusable_reason: string | null;
}

export interface DeployedNamedSetCatalogueItem {
  name: string;
  list_type: string;
  sql_usable: boolean;
  member_count: number;
  data_type: string | null;
  unusable_reason: string | null;
}

export interface DeployedNamedQueryCatalogueItem {
  name: string;
  shape: string;
  sql_usable: boolean;
  output_columns: DeployedNamedQueryOutputColumn[];
  unusable_reason: string | null;
}

export interface DeployedNamedQueryOutputColumn {
  name: string;
  type: string | null;
}

export interface DeployedNamedObjectsResponse {
  model_id: string;
  deployed_version_id: string | null;
  parameters: DeployedParameterCatalogueItem[];
  named_sets: DeployedNamedSetCatalogueItem[];
  named_queries: DeployedNamedQueryCatalogueItem[];
}
