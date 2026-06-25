/**
 * Agent service API — config and feedback calls.
 * Conversation CRUD, streaming, and turn access are handled by
 * agentChatAdapter.ts (shared-ui AgentChatAdapter contract).
 */
import { apiClient } from './client';
import type { AgentConfig } from '../types/tessallite';

function projectPath(projectId: string) {
  return `/api/v1/projects/${projectId}/agent`;
}

export async function getAgentConfig(projectId: string): Promise<AgentConfig> {
  return apiClient.get<AgentConfig>(`${projectPath(projectId)}/config`);
}

export async function sendFeedback(
  projectId: string,
  conversationId: string,
  turnId: string,
  rating: 'up' | 'down',
): Promise<void> {
  await apiClient.post(
    `${projectPath(projectId)}/conversations/${conversationId}/turns/${turnId}/feedback`,
    { vote: rating },
  );
}
