// ---------------------------------------------------------------------------
// Saved Queries
// ---------------------------------------------------------------------------
export interface SavedQueryCreate {
  name: string;
  description?: string;
  query_text: string;
  query_type?: string;
}
export interface SavedQueryUpdate {
  name?: string;
  description?: string;
  query_text?: string;
}
export interface SavedQuery {
  id: string;
  model_id: string;
  name: string;
  description: string | null;
  query_text: string;
  query_type: string;
  created_by: string;
  created_at: string;
  updated_at: string;
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
