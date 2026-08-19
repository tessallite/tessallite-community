import type { ConversationResponse, ConversationUpdatePayload } from "./conversation";
import type { TurnResponse } from "./turn";
import type { AgentVisibilityConfig, AgentPersona, SelectableModel } from "./config";

export interface CreateConversationOptions {
  personaId?: string | null;
  pinnedModelId?: string | null;
  modelId?: string | null;
}

export interface AgentChatAdapter {
  getConversations(projectId: string): Promise<ConversationResponse[]>;

  createConversation(
    projectId: string,
    options?: CreateConversationOptions,
  ): Promise<ConversationResponse>;

  getConversation(
    projectId: string,
    conversationId: string,
  ): Promise<ConversationResponse>;

  updateConversation(
    projectId: string,
    conversationId: string,
    data: ConversationUpdatePayload,
  ): Promise<ConversationResponse>;

  deleteConversation(
    projectId: string,
    conversationId: string,
  ): Promise<void>;

  getTurns(
    projectId: string,
    conversationId: string,
  ): Promise<TurnResponse[]>;

  // Bug-6521 — `idempotencyKey` is generated once per logical send (above the
  // retry loop) and forwarded as the `Idempotency-Key` header so the backend
  // dedupes the turn reservation across automatic retries. Optional so keyless
  // callers (and adapters that do not yet forward it) still compile.
  streamMessageRaw(
    projectId: string,
    conversationId: string,
    text: string,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<Response>;

  submitFeedback(
    projectId: string,
    conversationId: string,
    turnId: string,
    vote: "up" | "down",
    comment?: string,
  ): Promise<void>;

  getSelectableModels(projectId: string): Promise<SelectableModel[]>;

  getConfig?(projectId: string): Promise<AgentVisibilityConfig | null>;
  getPersonas?(projectId: string): Promise<AgentPersona[]>;
}
