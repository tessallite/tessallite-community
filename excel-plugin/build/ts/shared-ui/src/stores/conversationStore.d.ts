export interface ConversationState {
    activeConversationId: string | null;
    draftTitleCandidate: string | null;
    isStreaming: boolean;
    pendingModelId: string | null;
    pendingPersonaId: string | null;
    setActiveConversation: (id: string | null) => void;
    startNewConversation: () => void;
    /** Full session teardown -- clears ALL state including pending model/persona.
     *  Use for logout / unauthorized / session-switch, NOT for "New conversation". */
    resetSession: () => void;
    setDraftTitleCandidate: (text: string | null) => void;
    setStreaming: (v: boolean) => void;
    setPendingModelId: (id: string | null) => void;
    setPendingPersonaId: (id: string | null) => void;
    resetStreamingState: () => void;
}
export declare const useConversationStore: import("zustand").UseBoundStore<import("zustand").StoreApi<ConversationState>>;
