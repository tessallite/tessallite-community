import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";
import type { AgentChatAdapter } from "../types/adapter";
import type { AgentVisibilityConfig } from "../types/config";
import { useConversationStore } from "../stores/conversationStore";

export interface ChatContextValue {
  adapter: AgentChatAdapter;
  t: (key: string, params?: Record<string, string | number>) => string;
  projectId: string;
  config: AgentVisibilityConfig | null;
  isEmbed?: boolean;
  activeModelId?: string | null;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({
  children,
  ...value
}: ChatContextValue & { children: ReactNode }) {
  // Bug-7737 — the conversation store is a module-global Zustand store shared by
  // every host. Hosts mount ChatProvider on a stable route (e.g. the main app's
  // `/tenants/:t/projects/:p/agent` route) whose component instance survives a
  // project switch, so the store's `activeConversationId` (and pending
  // model/persona) from the previous project would otherwise leak into the new
  // project and make ChatCanvas fetch turns for a conversation that does not
  // belong to it. Resetting on projectId CHANGE (not first mount) clears that
  // stale state for all hosts, while leaving a URL/host-driven conversation
  // restore on initial mount untouched.
  const resetSession = useConversationStore((s) => s.resetSession);
  const prevProjectIdRef = useRef<string | null>(value.projectId ?? null);
  useEffect(() => {
    if (prevProjectIdRef.current !== value.projectId) {
      if (prevProjectIdRef.current !== null) {
        resetSession();
      }
      prevProjectIdRef.current = value.projectId ?? null;
    }
  }, [value.projectId, resetSession]);

  return (
    <ChatContext.Provider value={value}>{children}</ChatContext.Provider>
  );
}

export function useChatContext(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) {
    throw new Error("useChatContext must be used within a ChatProvider");
  }
  return ctx;
}
