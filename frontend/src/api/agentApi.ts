import axios from "axios";
import { agentServiceBaseUrl } from "./apiBase";

function getCsrfToken(): string | undefined {
  return document.cookie
    .split("; ")
    .find((row) => row.startsWith("csrf_token="))
    ?.split("=")[1];
}

const agent = axios.create({
  baseURL: agentServiceBaseUrl(),
  headers: { "Content-Type": "application/json" },
  withCredentials: true,
});

agent.interceptors.request.use((config) => {
  const csrf = getCsrfToken();
  if (csrf) config.headers["X-CSRF-Token"] = csrf;
  return config;
});

export type AgentConfig = {
  id?: string;
  project_id?: string;
  enabled: boolean;
  display_name: string | null;
  project_brief: string | null;
  agent_role: string;
  tone_preset: "professional" | "friendly";
  tone_overrides: string | null;
  brand_guidelines: string | null;
  safety_policy: string | null;
  content_rules: string | null;
  default_locale: string | null;
  disclosure_text: string | null;
  webhook_url: string | null;
  // Bug-8553 — true only on the save response that rotated an existing
  // receiver secret; ordinary GET responses return false.
  webhook_secret_rotated: boolean;
  // Bug-8411 — which agent events the webhook receiver subscribes to.
  // ["*"] (the default) means every event; null means the project predates
  // the column and is treated by the dispatcher as "every event" too.
  webhook_event_filters: string[] | null;
  primary_model_id: string | null;
  answer_llm_config_id: string | null;
  judge_llm_config_id: string | null;
  aggregate_llm_config_id: string | null;
  glossary_llm_config_id: string | null;
  judge_mode: "async" | "sync";
  judge_rubric_id: string | null;
  judge_block_visibility: "transparent" | "opaque";
  show_thought_process: boolean;
  show_semantic_query: boolean;
  show_physical_query: boolean;
  feedback_enabled: boolean;
  conversation_retention_days: number;
  enable_agent_log_screen: boolean;
  session_history_depth: number;
  daily_token_budget: number;
  daily_cost_budget_usd: number;
  max_query_complexity: number;
  agent_output_format: "json" | "plain" | "markup" | "html" | "rich_html";
  chart_type_selector: "none" | "llm" | "auto";
  chart_renderer: "echarts" | "html";
  chart_max_rows: number;
  chart_color_palette: "default" | "tessallite" | "muted" | "high_contrast" | "colorblind_safe";
  chart_size: "sm" | "md" | "lg";
  include_data_table: boolean;
  max_compound_steps: number;
};

export type AgentModelContext = {
  project_id?: string;
  model_id?: string;
  model_overview: string | null;
  analytical_capabilities: string | null;
  abbreviation_conflict_rules: string | null;
  example_questions: { q: string; decomposition: string }[];
  aggregates_summary?: unknown[];
  calendar_aliases?: unknown[];
  dimension_aliases?: unknown[];
};

// Bug-8181 — mirrors shared-ui's Citation type (types/turn.ts) field-for-field,
// same pattern this file already uses for the rest of AgentTurn/TurnResponse.
// Names MUST match the backend citation dict exactly
// (services/agent-service/src/citations/builder.py).
export type AgentCitation = {
  kind: "measure" | "dimension";
  id: string;
  name: string;
  display_name: string;
  value: number | string | null;
  definition: string | null;
  route_type: string | null;
  filter_grain: string | null;
};

export type RubricSection = {
  title: string;
  body?: string;
  bullets?: string[];
};

export type RubricBody = {
  name: string;
  sections: RubricSection[];
};

export type Rubric = RubricBody & { id: string };

export type AgentTurn = {
  id: string;
  conversation_id: string;
  turn_index: number;
  user_message: string;
  answer_text: string | null;
  status: string;
  latency_ms: number;
  llm_plan: Record<string, unknown> | null;
  thought_summary: string | null;
  semantic_query: Record<string, unknown> | null;
  routed_sql: string | null;
  route: string | null;
  citations: AgentCitation[] | null;
  user_feedback: { vote?: string; comment?: string | null; by?: string } | null;
  judge_verdict?: string | null;
  judge_reasoning?: string | null;
  judge_metrics?: Record<string, number> | null;
  guardrail_actions?: Array<Record<string, unknown>> | null;
  usage_input_tokens?: number;
  usage_output_tokens?: number;
  rendered_output?: string | null;
  judge_pending?: boolean;
  provider?: string | null;
  chart_type?: string | null;
  calculation_steps?: AgentCalculationStep[] | null;
  query_result_sample?: Array<Record<string, unknown>> | null;
};

export type AgentCalculationStep = {
  step_number?: number;
  description?: string;
  name?: string;
  measure?: string | null;
  value?: unknown;
  formatted_value?: string;
  formula?: string;
};

export type RecipeStep = {
  name: string;
  model_id: string;
  measures: string[];
  dimensions: string[];
  filters: { name: string; op: string; value?: unknown }[];
  limit: number;
};

export type RecipeParameter = {
  name: string;
  description: string | null;
  resolves_to_glossary_entity: boolean;
};

// Combine expression as a typed semantic tree (Bug-5346) — carried as data,
// never a formula string. A node is exactly one of: a constant, a reference to
// a step's measure, or an operation over child nodes.
export type ExprNode =
  | { const: number | string | boolean }
  | { ref: { step: string; measure: string } }
  | { op: string; args: ExprNode[] };

export type RecipeBody = {
  name: string;
  description: string | null;
  parameters: RecipeParameter[];
  steps: RecipeStep[];
  combine: ExprNode | null;
  notes: string | null;
};

export type Recipe = RecipeBody & { id: string };

export type AgentKpis = {
  window_days: number;
  total_turns: number;
  ok_turns: number;
  refused_turns: number;
  citations_rate: number;
  aggregate_route_rate: number;
  judge_block_rate: number;
  feedback_up: number;
  feedback_down: number;
  dlq_depth: number;
};

// F-023-01: `original_answer_blocked` is intentionally omitted from this
// type. The role-gated admin calibration endpoint does serve it, but no
// SPA component may bind or render blocked originals — a future
// calibration screen must re-add the field deliberately, behind that
// review.
export type CalibrationRow = {
  turn_id: string;
  conversation_id: string;
  created_at: string;
  user_message: string;
  answer_text: string | null;
  status: string;
  judge_verdict: string | null;
  judge_reasoning: string | null;
  judge_metrics: Record<string, number> | null;
};

export type CostDay = {
  date: string;
  input_tokens: number;
  output_tokens: number;
  estimated_usd: number;
};

export type CostReport = {
  window_days: number;
  total_input_tokens: number;
  total_output_tokens: number;
  estimated_usd: number;
  per_day: CostDay[];
};

export type EvalFieldComparison = {
  field_name: string;
  matched: boolean;
  expected: unknown;
  actual: unknown;
  detail: string | null;
};

export type EvalDecompositionComparison = {
  matched: boolean;
  skipped: boolean;
  skip_reason?: string | null;
  fields?: EvalFieldComparison[];
};

export type EvalReportRow = {
  model_id: string;
  question: string;
  expected_decomposition: unknown;
  status: string;
  plan: Record<string, unknown> | null;
  answer_text: string | null;
  error: string | null;
  decomposition_match: boolean | null;
  decomposition_comparison: EvalDecompositionComparison | null;
};

// Mirrors EvalReportOut in agent-service src/api/eval.py. The four fields
// below the rows were produced by the server and declared nowhere on this side,
// so no screen could show them — in particular `regressed`, which the server
// documents as the flag a client gating on eval accuracy MUST treat as a failed
// run regardless of the ok/refused/error counts.
export type EvalReport = {
  project_id: string;
  total: number;
  ok: number;
  refused: number;
  clarify: number;
  error: number;
  rows: EvalReportRow[];
  accuracy_score: number | null;
  decomposition_regressions: number;
  /** Set to the reason when the run stopped early on an exhausted budget. */
  budget_stopped: string | null;
  decomposition_compared: number;
  decomposition_unparseable: number;
  regressed: boolean;
};

export type WebhookEventType = {
  value: string;
  label: string;
};

export type WebhookDlqRow = {
  id: string;
  event_type: string;
  // Bug-8350 — the raw destination URL is never returned (it may embed a
  // bearer token or API key, in the path as much as the query string);
  // only a sanitised scheme://host[:port] hint is.
  target_host: string | null;
  attempt_count: number;
  last_status_code: number | null;
  last_error: string | null;
  first_attempted_at: string;
  last_attempted_at: string;
  resolved_at: string | null;
  payload: Record<string, unknown>;
};

export type AgentConversation = {
  id: string;
  project_id: string;
  caller_kind: string;
  caller_ref: string;
  persona_id: string | null;
  pinned_model_id: string | null;
  title: string | null;
  pinned_at: string | null;
  started_at: string;
  last_active_at: string;
  deleted_at: string | null;
};

export type PersonaModelScope = {
  model_id: string;
  included_measure_ids: string[];
  included_dimension_ids: string[];
};

export type AgentPersona = {
  id: string;
  project_id: string;
  name: string;
  slug: string;
  description: string | null;
  model_scopes: PersonaModelScope[];
};

export type LogTurnRow = {
  turn_id: string;
  conversation_id: string;
  turn_index: number;
  created_at: string;
  caller_ref: string;
  caller_kind: string;
  user_message: string;
  thought_summary: string | null;
  llm_plan: Record<string, unknown> | null;
  semantic_query: Record<string, unknown> | null;
  routed_sql: string | null;
  route: string | null;
  query_result_rows: number | null;
  answer_text: string | null;
  citations: AgentCitation[] | null;
  judge_verdict: string | null;
  judge_reasoning: string | null;
  judge_metrics: Record<string, number> | null;
  guardrail_actions: Array<Record<string, unknown>> | null;
  usage_input_tokens: number;
  usage_output_tokens: number;
  latency_ms: number;
  status: string;
  user_feedback: { vote?: string; comment?: string | null; by?: string } | null;
  prompt_messages: Record<string, string> | null;
  llm_raw_response: string | null;
};

export type ConversationStats = {
  total: number;
  oldest_at: string | null;
};

export type PurgeResponse = {
  deleted_conversations: number;
  older_than_days: number;
};

export type LogFilters = {
  date_from?: string;
  date_to?: string;
  caller_ref?: string;
  q?: string;
  page?: number;
  page_size?: number;
};

export type LogResponse = {
  items: LogTurnRow[];
  total: number;
  page: number;
  page_size: number;
};

export const DEFAULT_AGENT_CONFIG: AgentConfig = {
  enabled: false,
  display_name: null,
  project_brief: null,
  agent_role: "data analyst",
  tone_preset: "professional",
  tone_overrides: null,
  brand_guidelines: null,
  safety_policy: null,
  content_rules: null,
  default_locale: "en-GB",
  disclosure_text: null,
  webhook_url: null,
  webhook_secret_rotated: false,
  webhook_event_filters: ["*"],
  primary_model_id: null,
  answer_llm_config_id: null,
  judge_llm_config_id: null,
  aggregate_llm_config_id: null,
  glossary_llm_config_id: null,
  // F-023-29 / Bug-8148 — default is validated-first ("sync"): the answer is
  // validated before it is shown. "async" is an explicit lower-assurance override.
  judge_mode: "sync",
  judge_rubric_id: null,
  judge_block_visibility: "transparent",
  show_thought_process: true,
  show_semantic_query: true,
  show_physical_query: true,
  feedback_enabled: true,
  conversation_retention_days: 30,
  enable_agent_log_screen: false,
  session_history_depth: 20,
  daily_token_budget: 0,
  daily_cost_budget_usd: 0,
  max_query_complexity: 0,
  agent_output_format: "json",
  chart_type_selector: "auto",
  chart_renderer: "echarts",
  chart_max_rows: 500,
  chart_color_palette: "default",
  chart_size: "md",
  include_data_table: true,
  max_compound_steps: 3,
};

export const agentApi = {
  // Config
  getConfig: (projectId: string) =>
    agent
      .get<AgentConfig>(`/api/v1/projects/${projectId}/agent/config`)
      .then((r) => r.data)
      .catch((err) => {
        if (err.response?.status === 404) return null;
        throw err;
      }),
  upsertConfig: (projectId: string, body: AgentConfig) =>
    agent
      .put<AgentConfig>(`/api/v1/projects/${projectId}/agent/config`, body)
      .then((r) => r.data),
  patchConfig: (projectId: string, body: Partial<AgentConfig>) =>
    agent
      .patch<AgentConfig>(`/api/v1/projects/${projectId}/agent/config`, body)
      .then((r) => r.data),

  // Allow-list
  listAllowList: (projectId: string) =>
    agent
      .get<string[]>(`/api/v1/projects/${projectId}/agent/models`)
      .then((r) => r.data),
  replaceAllowList: (projectId: string, modelIds: string[]) =>
    agent
      .put<string[]>(`/api/v1/projects/${projectId}/agent/models`, {
        model_ids: modelIds,
      })
      .then((r) => r.data),

  // Per-model context
  getModelContext: (projectId: string, modelId: string) =>
    agent
      .get<AgentModelContext>(
        `/api/v1/projects/${projectId}/agent/model-context/${modelId}`,
      )
      .then((r) => r.data)
      .catch((err) => {
        if (err.response?.status === 404) return null;
        throw err;
      }),
  upsertModelContext: (
    projectId: string,
    modelId: string,
    body: AgentModelContext,
  ) =>
    agent
      .put<AgentModelContext>(
        `/api/v1/projects/${projectId}/agent/model-context/${modelId}`,
        body,
      )
      .then((r) => r.data),

  // Conversations + turns (Phase A canned-response path)
  createConversation: (
    projectId: string,
    payload?: { persona_id?: string | null; pinned_model_id?: string | null },
  ) =>
    agent
      .post<AgentConversation>(
        `/api/v1/projects/${projectId}/agent/conversations`,
        payload ?? {},
      )
      .then((r) => r.data),
  listConversations: (projectId: string) =>
    agent
      .get<AgentConversation[]>(
        `/api/v1/projects/${projectId}/agent/conversations`,
      )
      .then((r) => r.data),
  getConversation: (projectId: string, conversationId: string) =>
    agent
      .get<AgentConversation>(
        `/api/v1/projects/${projectId}/agent/conversations/${conversationId}`,
      )
      .then((r) => r.data),
  deleteConversation: (projectId: string, conversationId: string) =>
    agent.delete(
      `/api/v1/projects/${projectId}/agent/conversations/${conversationId}`,
    ),
  patchConversation: (
    projectId: string,
    conversationId: string,
    patch: { title?: string; pinned?: boolean; persona_id?: string | null },
  ) =>
    agent
      .patch<AgentConversation>(
        `/api/v1/projects/${projectId}/agent/conversations/${conversationId}`,
        patch,
      )
      .then((r) => r.data),
  listTurns: (projectId: string, conversationId: string) =>
    agent
      .get<AgentTurn[]>(
        `/api/v1/projects/${projectId}/agent/conversations/${conversationId}/turns`,
      )
      .then((r) => r.data),
  sendMessage: (projectId: string, conversationId: string, text: string) =>
    agent
      .post<AgentTurn>(
        `/api/v1/projects/${projectId}/agent/conversations/${conversationId}/messages`,
        { text },
      )
      .then((r) => r.data),
  streamMessage: async (
    projectId: string,
    conversationId: string,
    text: string,
    onEvent: (name: string, data: Record<string, unknown>) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const csrf = getCsrfToken();
    const url =
      `${agentServiceBaseUrl()}/api/v1/projects/${projectId}` +
      `/agent/conversations/${conversationId}/messages/stream`;
    const res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      },
      body: JSON.stringify({ text }),
      signal,
    });
    if (!res.ok || !res.body) {
      throw new Error(`Stream failed: HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sepIdx: number;
      while ((sepIdx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);
        if (!frame.trim() || frame.startsWith(":")) continue;
        let eventName = "message";
        let dataLine = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLine += line.slice(5).trim();
        }
        if (!dataLine) {
          onEvent(eventName, {});
          continue;
        }
        try {
          onEvent(eventName, JSON.parse(dataLine));
        } catch {
          onEvent(eventName, { raw: dataLine });
        }
      }
    }
  },
  // Judge rubrics (B4)
  listRubrics: (projectId: string) =>
    agent
      .get<Rubric[]>(`/api/v1/projects/${projectId}/agent/rubrics`)
      .then((r) => r.data),
  createRubric: (projectId: string, body: RubricBody) =>
    agent
      .post<Rubric>(`/api/v1/projects/${projectId}/agent/rubrics`, body)
      .then((r) => r.data),
  updateRubric: (projectId: string, rubricId: string, body: RubricBody) =>
    agent
      .put<Rubric>(
        `/api/v1/projects/${projectId}/agent/rubrics/${rubricId}`,
        body,
      )
      .then((r) => r.data),
  deleteRubric: (projectId: string, rubricId: string) =>
    agent.delete(`/api/v1/projects/${projectId}/agent/rubrics/${rubricId}`),
  bulkImportRubrics: (projectId: string, rubrics: RubricBody[]) =>
    agent
      .post<Rubric[]>(
        `/api/v1/projects/${projectId}/agent/rubrics/bulk-import`,
        { rubrics },
      )
      .then((r) => r.data),

  // Auto-derived per-model context (B4)
  refreshDerivedContext: (projectId: string, modelId: string) =>
    agent
      .post(
        `/api/v1/projects/${projectId}/agent/model-context/${modelId}/refresh-derived`,
      )
      .then((r) => r.data),
  refreshAllDerivedContext: (projectId: string) =>
    agent
      .post(`/api/v1/projects/${projectId}/agent/refresh-derived`)
      .then((r) => r.data),

  // KPIs + eval (C3)
  getKpis: (projectId: string, windowDays = 30) =>
    agent
      .get<AgentKpis>(
        `/api/v1/projects/${projectId}/agent/kpis?window_days=${windowDays}`,
      )
      .then((r) => r.data),
  runEval: (projectId: string) =>
    agent
      .post<EvalReport>(`/api/v1/projects/${projectId}/agent/eval/run`)
      .then((r) => r.data),
  getCalibration: (projectId: string, limit = 20) =>
    agent
      .get<CalibrationRow[]>(
        `/api/v1/projects/${projectId}/agent/calibration?limit=${limit}`,
      )
      .then((r) => r.data),
  getCost: (projectId: string, windowDays = 30) =>
    agent
      .get<CostReport>(
        `/api/v1/projects/${projectId}/agent/cost?window_days=${windowDays}`,
      )
      .then((r) => r.data),

  // Webhooks (C2)
  // Bug-8411 — the event catalogue is served by the backend so the Settings
  // checkboxes can never drift from what the dispatcher actually emits.
  listWebhookEventTypes: (projectId: string) =>
    agent
      .get<WebhookEventType[]>(
        `/api/v1/projects/${projectId}/agent/webhook/event-types`,
      )
      .then((r) => r.data),
  rotateWebhookSecret: (projectId: string) =>
    agent
      .post<{ signing_secret: string }>(
        `/api/v1/projects/${projectId}/agent/webhook/rotate-secret`,
      )
      .then((r) => r.data),
  listWebhookDlq: (projectId: string) =>
    agent
      .get<WebhookDlqRow[]>(`/api/v1/projects/${projectId}/agent/webhook/dlq`)
      .then((r) => r.data),
  retryWebhookDlq: (projectId: string, dlqId: string) =>
    agent.post(`/api/v1/projects/${projectId}/agent/webhook/dlq/${dlqId}/retry`),
  discardWebhookDlq: (projectId: string, dlqId: string) =>
    agent.delete(`/api/v1/projects/${projectId}/agent/webhook/dlq/${dlqId}`),

  // Conversation purge (E3)
  getConversationStats: (projectId: string) =>
    agent
      .get<ConversationStats>(
        `/api/v1/admin/agent/conversations/stats?project_id=${projectId}`,
      )
      .then((r) => r.data),
  purgeConversations: (projectId: string, olderThanDays: number) =>
    agent
      .delete<PurgeResponse>(
        `/api/v1/admin/agent/conversations/purge?project_id=${projectId}&older_than_days=${olderThanDays}`,
      )
      .then((r) => r.data),

  getSelectableModels: (projectId: string) =>
    agent
      .get<{ id: string; name: string }[]>(
        `/api/v1/projects/${projectId}/agent/selectable-models`,
      )
      .then((r) => r.data),

  // Cross-model recipes (B3)
  listRecipes: (projectId: string) =>
    agent
      .get<Recipe[]>(`/api/v1/projects/${projectId}/agent/recipes`)
      .then((r) => r.data),
  createRecipe: (projectId: string, body: RecipeBody) =>
    agent
      .post<Recipe>(`/api/v1/projects/${projectId}/agent/recipes`, body)
      .then((r) => r.data),
  updateRecipe: (projectId: string, recipeId: string, body: RecipeBody) =>
    agent
      .put<Recipe>(
        `/api/v1/projects/${projectId}/agent/recipes/${recipeId}`,
        body,
      )
      .then((r) => r.data),
  deleteRecipe: (projectId: string, recipeId: string) =>
    agent.delete(`/api/v1/projects/${projectId}/agent/recipes/${recipeId}`),

  submitFeedback: (
    projectId: string,
    conversationId: string,
    turnId: string,
    vote: "up" | "down",
    comment?: string,
  ) =>
    agent.post(
      `/api/v1/projects/${projectId}/agent/conversations/${conversationId}/turns/${turnId}/feedback`,
      { vote, comment },
    ),

  getLog: (projectId: string, filters: LogFilters = {}) => {
    const params = new URLSearchParams();
    if (filters.date_from) params.set("date_from", filters.date_from);
    if (filters.date_to) params.set("date_to", filters.date_to);
    if (filters.caller_ref) params.set("caller_ref", filters.caller_ref);
    if (filters.q) params.set("q", filters.q);
    params.set("page", String(filters.page ?? 1));
    params.set("page_size", String(filters.page_size ?? 25));
    return agent
      .get<LogResponse>(
        `/api/v1/projects/${projectId}/agent/log?${params.toString()}`,
      )
      .then((r) => r.data);
  },

  // Personas
  listPersonas: (projectId: string) =>
    agent
      .get<AgentPersona[]>(
        `/api/v1/projects/${projectId}/agent/personas`,
      )
      .then((r) => r.data),
  createPersona: (
    projectId: string,
    body: { name: string; slug?: string; description?: string; model_scopes?: PersonaModelScope[] },
  ) =>
    agent
      .post<AgentPersona>(
        `/api/v1/projects/${projectId}/agent/personas`,
        body,
      )
      .then((r) => r.data),
  updatePersona: (
    projectId: string,
    personaId: string,
    body: { name?: string; description?: string; model_scopes?: PersonaModelScope[] },
  ) =>
    agent
      .patch<AgentPersona>(
        `/api/v1/projects/${projectId}/agent/personas/${personaId}`,
        body,
      )
      .then((r) => r.data),
  deletePersona: (projectId: string, personaId: string) =>
    agent.delete(
      `/api/v1/projects/${projectId}/agent/personas/${personaId}`,
    ),
};
