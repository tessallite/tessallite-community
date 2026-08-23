export interface ConversationResponse {
  id: string;
  project_id: string;
  title: string | null;
  pinned_at: string | null;
  started_at: string;
  last_active_at: string;
  deleted_at: string | null;
  persona_id: string | null;
  pinned_model_id: string | null;
}

export interface ConversationUpdatePayload {
  title?: string | null;
  pinned?: boolean;
  persona_id?: string | null;
  pinned_model_id?: string | null;
}
