import type {
  AgentChatAdapter,
  ConversationResponse,
  ConversationUpdatePayload,
  TurnResponse,
  SelectableModel,
  AgentConfig,
  AgentPersona,
  CreateConversationOptions,
} from "@tessallite/shared-ui";
import { agentApi, type AgentConversation, type AgentTurn } from "./agentApi";
import { agentServiceBaseUrl } from "./apiBase";

function getCsrfToken(): string | undefined {
  return document.cookie
    .split("; ")
    .find((row) => row.startsWith("csrf_token="))
    ?.split("=")[1];
}

function toConversationResponse(c: AgentConversation): ConversationResponse {
  return {
    id: c.id,
    project_id: c.project_id,
    title: c.title,
    pinned_at: c.pinned_at,
    started_at: c.started_at,
    last_active_at: c.last_active_at,
    deleted_at: c.deleted_at,
    persona_id: c.persona_id,
    pinned_model_id: c.pinned_model_id ?? null,
  };
}

function toTurnResponse(t: AgentTurn): TurnResponse {
  return {
    id: t.id,
    conversation_id: t.conversation_id,
    turn_index: t.turn_index,
    user_message: t.user_message,
    answer_text: t.answer_text,
    status: t.status,
    latency_ms: t.latency_ms,
    thought_summary: t.thought_summary,
    semantic_query: t.semantic_query,
    routed_sql: t.routed_sql,
    route: t.route,
    citations: t.citations ?? null,
    user_feedback: t.user_feedback
      ? { vote: t.user_feedback.vote ?? "", comment: t.user_feedback.comment ?? null }
      : null,
    judge_verdict: t.judge_verdict ?? null,
    judge_reasoning: t.judge_reasoning ?? null,
    judge_metrics: t.judge_metrics ?? null,
    guardrail_actions: t.guardrail_actions ?? null,
    usage_input_tokens: t.usage_input_tokens ?? null,
    usage_output_tokens: t.usage_output_tokens ?? null,
    rendered_output: t.rendered_output ?? null,
    llm_plan: t.llm_plan,
    query_result_rows: null,
    query_result_sample: t.query_result_sample ?? null,
    calculation_steps: t.calculation_steps
      ? t.calculation_steps.map((cs, i) => ({
          step: cs.step_number ?? i + 1,
          ...cs,
        }))
      : null,
    chart_type: t.chart_type ?? null,
    provider: t.provider ?? null,
    judge_pending: t.judge_pending,
  };
}

export const mainAppAdapter: AgentChatAdapter = {
  getConversations: async (projectId) => {
    const list = await agentApi.listConversations(projectId);
    return list.map(toConversationResponse);
  },

  createConversation: async (
    projectId,
    options?: CreateConversationOptions,
  ) => {
    const payload: { persona_id?: string | null; pinned_model_id?: string | null } = {};
    if (options?.personaId) payload.persona_id = options.personaId;
    if (options?.pinnedModelId) payload.pinned_model_id = options.pinnedModelId;
    const conv = await agentApi.createConversation(
      projectId,
      Object.keys(payload).length > 0 ? payload : undefined,
    );
    return toConversationResponse(conv);
  },

  getConversation: async (projectId, conversationId) => {
    const conv = await agentApi.getConversation(projectId, conversationId);
    return toConversationResponse(conv);
  },

  updateConversation: async (
    projectId,
    conversationId,
    data: ConversationUpdatePayload,
  ) => {
    const patch: Record<string, unknown> = {};
    if (data.title !== undefined) patch.title = data.title;
    if (data.pinned_model_id !== undefined)
      patch.pinned_model_id = data.pinned_model_id;
    const conv = await agentApi.patchConversation(
      projectId,
      conversationId,
      patch as { title?: string; pinned?: boolean; persona_id?: string | null },
    );
    return toConversationResponse(conv);
  },

  deleteConversation: async (projectId, conversationId) => {
    await agentApi.deleteConversation(projectId, conversationId);
  },

  getTurns: async (projectId, conversationId) => {
    const turns = await agentApi.listTurns(projectId, conversationId);
    return turns.map(toTurnResponse);
  },

  streamMessageRaw: async (projectId, conversationId, text, signal, idempotencyKey) => {
    const csrf = getCsrfToken();
    const url =
      `${agentServiceBaseUrl()}/api/v1/projects/${projectId}` +
      `/agent/conversations/${conversationId}/messages/stream`;
    return fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(csrf ? { "X-CSRF-Token": csrf } : {}),
        // Bug-6521 — forward the per-send idempotency key so the backend
        // dedupes the turn reservation across streaming retries.
        ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      },
      body: JSON.stringify({ text }),
      signal,
    });
  },

  submitFeedback: async (projectId, conversationId, turnId, vote, comment) => {
    await agentApi.submitFeedback(
      projectId,
      conversationId,
      turnId,
      vote,
      comment,
    );
  },

  getSelectableModels: async (projectId) => {
    return agentApi.getSelectableModels(projectId);
  },

  getConfig: async (projectId) => {
    return agentApi.getConfig(projectId) as Promise<AgentConfig | null>;
  },

  getPersonas: async (projectId) => {
    const personas = await agentApi.listPersonas(projectId);
    return personas.map((p) => ({
      id: p.id,
      name: p.name,
      description: p.description,
      system_prompt: null,
      persona_type: "custom",
      scope_filter: null,
    })) as AgentPersona[];
  },
};
