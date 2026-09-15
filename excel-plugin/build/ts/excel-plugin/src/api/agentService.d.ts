import type { AgentConfig } from '../types/tessallite';
export declare function getAgentConfig(projectId: string, signal?: AbortSignal): Promise<AgentConfig>;
export declare function sendFeedback(projectId: string, conversationId: string, turnId: string, rating: 'up' | 'down'): Promise<void>;
