import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentTurn } from "./agentApi";

const listTurnsMock = vi.hoisted(() => vi.fn());
const createConversationMock = vi.hoisted(() => vi.fn());

vi.mock("./agentApi", () => ({
  agentApi: {
    listTurns: listTurnsMock,
    createConversation: createConversationMock,
  },
}));
vi.mock("./apiBase", () => ({
  agentServiceBaseUrl: () => "http://agent-service",
}));

import {
  mainAppAdapter,
  resetProjectPersonaWriteBarrier,
  setProjectPersonaWriteBarrier,
} from "./agentChatAdapter";

describe("agent chat adapter", () => {
  beforeEach(() => {
    resetProjectPersonaWriteBarrier();
    createConversationMock.mockReset();
  });

  it("Bug-8364: derives verifying state from persisted judge_pending status", async () => {
    listTurnsMock.mockResolvedValue([
      {
        id: "turn-1",
        conversation_id: "conversation-1",
        turn_index: 1,
        user_message: "Show revenue",
        answer_text: null,
        status: "judge_pending",
        latency_ms: 0,
        judge_pending: undefined,
      } as AgentTurn,
    ]);

    const turns = await mainAppAdapter.getTurns("project-1", "conversation-1");

    expect(turns[0]?.judge_pending).toBe(true);
  });

  it("L13-9196-SPA: waits for a successful ProjectPersona PATCH before create", async () => {
    let release!: () => void;
    const patch = new Promise<void>((resolve) => { release = resolve; });
    createConversationMock.mockResolvedValue({ id: "c1", persona_id: "pp1" });
    setProjectPersonaWriteBarrier(patch);
    const pending = mainAppAdapter.createConversation("p1", { personaId: "pp1" });
    await Promise.resolve();
    expect(createConversationMock).not.toHaveBeenCalled();
    release();
    await pending;
    expect(createConversationMock).toHaveBeenCalledTimes(1);
  });

  it("L13-9196-SPA: rejects queued create and resets the barrier after PATCH failure", async () => {
    const failure = new Error("PATCH rejected");
    createConversationMock.mockResolvedValue({ id: "should-not-exist", persona_id: "pp1" });
    setProjectPersonaWriteBarrier(Promise.reject(failure));
    await expect(mainAppAdapter.createConversation("p1", { personaId: "pp1" }))
      .rejects.toThrow("PATCH rejected");
    expect(createConversationMock).not.toHaveBeenCalled();

    // Settlement clears the barrier, so a later operation can proceed.
    await mainAppAdapter.createConversation("p1", { personaId: "pp1" });
    expect(createConversationMock).toHaveBeenCalledTimes(1);
  });
});
