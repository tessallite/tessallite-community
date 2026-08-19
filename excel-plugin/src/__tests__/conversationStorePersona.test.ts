/**
 * Bug-7735: verify that startNewConversation() preserves the active persona
 * and model selection. The previous implementation cleared pendingPersonaId
 * and pendingModelId, silently dropping the user's persona and locking them
 * out of conversations created without the intended persona.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useConversationStore } from "@tessallite/shared-ui";

function getState() {
  return useConversationStore.getState();
}

beforeEach(() => {
  // Reset store to initial state between tests.
  useConversationStore.setState({
    activeConversationId: null,
    draftTitleCandidate: null,
    isStreaming: false,
    pendingModelId: null,
    pendingPersonaId: null,
  });
});

describe("Bug-7735 — startNewConversation preserves persona and model", () => {
  it("preserves pendingPersonaId after startNewConversation", () => {
    getState().setPendingPersonaId("persona-42");
    expect(getState().pendingPersonaId).toBe("persona-42");

    getState().startNewConversation();

    expect(getState().pendingPersonaId).toBe("persona-42");
  });

  it("preserves pendingModelId after startNewConversation", () => {
    getState().setPendingModelId("model-7");
    expect(getState().pendingModelId).toBe("model-7");

    getState().startNewConversation();

    expect(getState().pendingModelId).toBe("model-7");
  });

  it("clears conversationId, draft title, and streaming state", () => {
    getState().setActiveConversation("conv-1");
    getState().setDraftTitleCandidate("My Chat");
    getState().setStreaming(true);

    getState().startNewConversation();

    expect(getState().activeConversationId).toBeNull();
    expect(getState().draftTitleCandidate).toBeNull();
    expect(getState().isStreaming).toBe(false);
  });

  it("preserves both persona and model while clearing conversation state", () => {
    // Set up full state
    getState().setActiveConversation("conv-1");
    getState().setDraftTitleCandidate("Draft");
    getState().setStreaming(true);
    getState().setPendingPersonaId("persona-42");
    getState().setPendingModelId("model-7");

    getState().startNewConversation();

    // Conversation state cleared
    expect(getState().activeConversationId).toBeNull();
    expect(getState().draftTitleCandidate).toBeNull();
    expect(getState().isStreaming).toBe(false);

    // Persona and model preserved
    expect(getState().pendingPersonaId).toBe("persona-42");
    expect(getState().pendingModelId).toBe("model-7");
  });

  it("works correctly when persona/model are already null", () => {
    getState().setActiveConversation("conv-1");

    getState().startNewConversation();

    expect(getState().activeConversationId).toBeNull();
    expect(getState().pendingPersonaId).toBeNull();
    expect(getState().pendingModelId).toBeNull();
  });
});

describe("resetSession clears ALL state including persona and model", () => {
  it("clears pendingPersonaId and pendingModelId (unlike startNewConversation)", () => {
    getState().setActiveConversation("conv-1");
    getState().setDraftTitleCandidate("Draft");
    getState().setStreaming(true);
    getState().setPendingPersonaId("persona-42");
    getState().setPendingModelId("model-7");

    getState().resetSession();

    expect(getState().activeConversationId).toBeNull();
    expect(getState().draftTitleCandidate).toBeNull();
    expect(getState().isStreaming).toBe(false);
    expect(getState().pendingPersonaId).toBeNull();
    expect(getState().pendingModelId).toBeNull();
  });
});
