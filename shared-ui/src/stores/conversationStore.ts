import { create } from "zustand";

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

export const useConversationStore = create<ConversationState>((set) => ({
  activeConversationId: null,
  draftTitleCandidate: null,
  isStreaming: false,
  pendingModelId: null,
  pendingPersonaId: null,

  setActiveConversation: (id) => set({ activeConversationId: id }),
  startNewConversation: () =>
    set({
      activeConversationId: null,
      draftTitleCandidate: null,
      isStreaming: false,
      // Bug-7735: preserve pendingModelId and pendingPersonaId so the
      // user's active persona and model selection survive "New conversation".
      // Callers that genuinely need to clear these (project/model switch,
      // logout) do so explicitly via setActivePersonaId(null) which triggers
      // the useEffect sync in ExcelChatShell, or use resetSession().
    }),
  resetSession: () =>
    set({
      activeConversationId: null,
      draftTitleCandidate: null,
      isStreaming: false,
      pendingModelId: null,
      pendingPersonaId: null,
    }),
  setDraftTitleCandidate: (text) => set({ draftTitleCandidate: text }),
  setStreaming: (v) => set({ isStreaming: v }),
  setPendingModelId: (id) => set({ pendingModelId: id }),
  setPendingPersonaId: (id) => set({ pendingPersonaId: id }),
  resetStreamingState: () =>
    set({ isStreaming: false, draftTitleCandidate: null }),
}));
