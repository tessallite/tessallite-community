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
import { apiClient, streamRequest, ApiError } from "./client";
import { logError } from "../utils/diagnostics";
import type {
  AgentConfig as LocalAgentConfig,
  AgentPersona as LocalAgentPersona,
  AgentConversation,
} from "../types/tessallite";

function projectPath(projectId: string) {
  return `/api/v1/projects/${projectId}/agent`;
}

function toConversationResponse(c: AgentConversation): ConversationResponse {
  const raw = c as AgentConversation & Record<string, unknown>;
  return {
    id: raw.id,
    project_id: (raw.project_id as string) ?? "",
    title: raw.title ?? null,
    pinned_at: (raw.pinned_at as string) ?? null,
    started_at: (raw.started_at as string) ?? raw.created_at ?? new Date().toISOString(),
    last_active_at: (raw.last_active_at as string) ?? raw.created_at ?? new Date().toISOString(),
    deleted_at: (raw.deleted_at as string) ?? null,
    persona_id: raw.persona_id ?? null,
    pinned_model_id: (raw.pinned_model_id as string) ?? null,
  };
}

interface RawTurn {
  id: string;
  conversation_id?: string;
  turn_index?: number;
  user_message: string;
  answer_text: string | null;
  status?: string;
  latency_ms?: number | null;
  thought_summary?: string | null;
  semantic_query?: unknown;
  routed_sql?: string | null;
  route?: string | null;
  // Bug-8181 — mirrors shared-ui's Citation type (types/turn.ts) field-for-
  // field. Names MUST match the backend citation dict exactly
  // (services/agent-service/src/citations/builder.py).
  citations?: Array<{
    kind: string;
    id: string;
    name: string;
    display_name: string;
    value: unknown;
    definition?: string | null;
    route_type?: string | null;
    filter_grain?: string | null;
  }> | null;
  user_feedback?: { vote?: string; comment?: string | null } | null;
  judge_verdict?: string | null;
  judge_reasoning?: string | null;
  judge_metrics?: Record<string, number> | null;
  guardrail_actions?: unknown[] | null;
  usage_input_tokens?: number | null;
  usage_output_tokens?: number | null;
  rendered_output?: string | null;
  llm_plan?: unknown;
  query_result_rows?: number | null;
  query_result_sample?: Record<string, unknown>[] | null;
  calculation_steps?: Array<{ step_number?: number; description?: string; name?: string; measure?: string | null; value?: unknown; formatted_value?: string; formula?: string }> | null;
  chart_type?: string | null;
  provider?: string | null;
  judge_pending?: boolean;
}

function toTurnResponse(t: RawTurn): TurnResponse {
  return {
    id: t.id,
    conversation_id: t.conversation_id ?? "",
    turn_index: t.turn_index ?? 0,
    user_message: t.user_message,
    answer_text: t.answer_text,
    status: t.status ?? "completed",
    latency_ms: t.latency_ms ?? null,
    thought_summary: t.thought_summary ?? null,
    semantic_query: t.semantic_query ?? null,
    routed_sql: t.routed_sql ?? null,
    route: t.route ?? null,
    citations: t.citations?.map((c) => ({
      kind: c.kind as "measure" | "dimension",
      id: c.id,
      name: c.name,
      display_name: c.display_name,
      value: c.value as number | string | null,
      definition: c.definition ?? null,
      route_type: c.route_type ?? null,
      filter_grain: c.filter_grain ?? null,
    })) ?? null,
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
    llm_plan: t.llm_plan ?? null,
    query_result_rows: t.query_result_rows ?? null,
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

export function createExcelAdapter(activeModelId: () => string | null): AgentChatAdapter {
  return {
    getConversations: async (projectId) => {
      const list = await apiClient.get<AgentConversation[]>(
        `${projectPath(projectId)}/conversations`,
      );
      return list.map(toConversationResponse);
    },

    createConversation: async (projectId, options?: CreateConversationOptions) => {
      const payload: Record<string, unknown> = {};
      const modelId = options?.pinnedModelId ?? activeModelId();
      if (modelId) payload.pinned_model_id = modelId;
      if (options?.personaId) payload.persona_id = options.personaId;
      const conv = await apiClient.post<AgentConversation>(
        `${projectPath(projectId)}/conversations`,
        payload,
      );
      return toConversationResponse(conv);
    },

    getConversation: async (projectId, conversationId) => {
      const conv = await apiClient.get<AgentConversation>(
        `${projectPath(projectId)}/conversations/${conversationId}`,
      );
      return toConversationResponse(conv);
    },

    updateConversation: async (projectId, conversationId, data: ConversationUpdatePayload) => {
      const patch: Record<string, unknown> = {};
      if (data.title !== undefined) patch.title = data.title;
      if (data.pinned_model_id !== undefined) patch.pinned_model_id = data.pinned_model_id;
      const conv = await apiClient.patch<AgentConversation>(
        `${projectPath(projectId)}/conversations/${conversationId}`,
        patch,
      );
      return toConversationResponse(conv);
    },

    deleteConversation: async (projectId, conversationId) => {
      await apiClient.delete(
        `${projectPath(projectId)}/conversations/${conversationId}`,
      );
    },

    getTurns: async (projectId, conversationId) => {
      const turns = await apiClient.get<RawTurn[]>(
        `${projectPath(projectId)}/conversations/${conversationId}/turns`,
      );
      return turns.map(toTurnResponse);
    },

    streamMessageRaw: async (projectId, conversationId, text, signal, idempotencyKey) => {
      const path = `${projectPath(projectId)}/conversations/${conversationId}/messages/stream`;
      try {
        return await streamRequest(
          path,
          { text },
          signal,
          // Bug-6596: forward the per-send idempotency key as the
          // Idempotency-Key header so the backend dedupes the turn reservation
          // across streaming retries (mirrors the frontend adapter). Backward-
          // compatible — omitted when the caller passes no key.
          idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
        );
      } catch (err) {
        // Bug-7388: SSE stream drops (server error, token expiry, rate limit,
        // network failure) must not be silently swallowed. The shared-ui
        // stream driver (sendMessageStream) reconnects/retries on this throw,
        // but unlike apiClient.request, streamRequest does not record the drop
        // in the plugin diagnostics log — so a support user copying diagnostics
        // saw no trace of why a long agent response "finished early". Log the
        // drop reason (HTTP status when available, else the error message)
        // before re-raising so reconnect/backoff behaviour is unchanged.
        if (err instanceof DOMException && err.name === "AbortError") {
          // User-initiated cancel — not a fault, do not log as an error.
          throw err;
        }
        if (err instanceof ApiError) {
          logError(`SSE stream drop on message send: HTTP ${err.status} — ${err.message}`);
        } else {
          const reason = err instanceof Error ? err.message : String(err);
          logError(`SSE stream drop on message send: ${reason}`);
        }
        throw err;
      }
    },

    submitFeedback: async (projectId, conversationId, turnId, vote) => {
      await apiClient.post(
        `${projectPath(projectId)}/conversations/${conversationId}/turns/${turnId}/feedback`,
        { vote },
      );
    },

    getSelectableModels: async (projectId) => {
      try {
        return await apiClient.get<SelectableModel[]>(
          `${projectPath(projectId)}/selectable-models`,
        );
      } catch {
        return [];
      }
    },

    getConfig: async (projectId) => {
      try {
        const cfg = await apiClient.get<LocalAgentConfig>(`${projectPath(projectId)}/config`);
        return cfg as unknown as AgentConfig;
      } catch {
        return null;
      }
    },

    getPersonas: async (projectId) => {
      try {
        const personas = await apiClient.get<LocalAgentPersona[]>(
          `${projectPath(projectId)}/personas`,
        );
        return personas.map((p) => ({
          id: p.id,
          name: p.name,
          description: p.description,
          system_prompt: null,
          persona_type: "custom",
          scope_filter: null,
        })) as AgentPersona[];
      } catch {
        return [];
      }
    },
  };
}
