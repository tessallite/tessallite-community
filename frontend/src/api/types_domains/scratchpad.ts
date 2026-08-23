// ---------------------------------------------------------------------------
// Saved Queries
// ---------------------------------------------------------------------------
export interface SavedQueryCreate {
  name: string;
  description?: string;
  query_text: string;
  query_type?: string;
  is_shared?: boolean;
}
export interface SavedQueryUpdate {
  name?: string;
  description?: string;
  query_text?: string;
  is_shared?: boolean;
}
export interface SavedQuery {
  id: string;
  model_id: string;
  name: string;
  description: string | null;
  query_text: string;
  query_type: string;
  created_by: string;
  is_shared: boolean;
  created_at: string;
  updated_at: string;
  is_owner?: boolean;
  // Bug-5983: distinct from is_owner -- a modeler+ can also edit/delete a
  // saved query they do not own (backend `_require_owner_or_modeler`).
  // Edit/delete controls must gate on this field, not is_owner.
  can_edit?: boolean;
}

export interface ScratchpadMeasure {
  id: string;
  model_id: string;
  name: string;
  display_name: string | null;
  expression: string;
  data_type: string;
  format: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ScratchpadMeasureCreate {
  name: string;
  display_name?: string | null;
  expression: string;
  data_type?: string;
  format?: string | null;
}

export interface ScratchpadMeasureUpdate {
  name?: string;
  display_name?: string | null;
  expression?: string;
  data_type?: string;
  format?: string | null;
}

export interface ModelDocsResponse {
  markdown: string;
  generated_at: string;
}
