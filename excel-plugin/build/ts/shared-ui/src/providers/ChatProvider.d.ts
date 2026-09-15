import { type ReactNode } from "react";
import type { AgentChatAdapter } from "../types/adapter";
import type { AgentVisibilityConfig } from "../types/config";
export interface ChatContextValue {
    adapter: AgentChatAdapter;
    t: (key: string, params?: Record<string, string | number>) => string;
    projectId: string;
    config: AgentVisibilityConfig | null;
    isEmbed?: boolean;
    activeModelId?: string | null;
}
export declare function ChatProvider({ children, ...value }: ChatContextValue & {
    children: ReactNode;
}): import("react").JSX.Element;
export declare function useChatContext(): ChatContextValue;
