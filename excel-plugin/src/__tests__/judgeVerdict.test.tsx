/**
 * F-025-05 — judge verdicts render from the REAL response shapes.
 *
 * Tests the shared-ui JudgeVerdictStrip component as consumed by the Excel
 * plugin. Verifies that the verdict label, reasoning, and per-rubric-section
 * metric bars render from the real TurnResponse shape (not fictional
 * confidence/rubric_score values).
 *
 * The judge shapes are taken from the real producers:
 *  - judge_metrics keys are rubric SECTION TITLES with 0.0-1.0 scores
 *    (agent-service judge/judge.py), e.g. "Factual accuracy": 1.0.
 *  - Scores display as 0-5 scale (Math.round(v * 5)/5).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";
import { JudgeVerdictStrip } from "@tessallite/shared-ui/components/JudgeVerdictStrip";
import { ChatProvider } from "@tessallite/shared-ui/providers/ChatProvider";
import type { AgentChatAdapter } from "@tessallite/shared-ui/types/adapter";
import type { TurnResponse } from "@tessallite/shared-ui/types/turn";
import { chatT } from "../i18n/chatStrings";

const NOOP_ADAPTER: AgentChatAdapter = {
  getConversations: vi.fn().mockResolvedValue([]),
  createConversation: vi.fn().mockResolvedValue({ id: "c1", title: null, pinned_model_id: null }),
  getConversation: vi.fn().mockResolvedValue({ id: "c1", title: null, pinned_model_id: null }),
  updateConversation: vi.fn().mockResolvedValue({ id: "c1", title: null, pinned_model_id: null }),
  deleteConversation: vi.fn().mockResolvedValue(undefined),
  getTurns: vi.fn().mockResolvedValue([]),
  streamMessageRaw: vi.fn().mockResolvedValue(new Response()),
  submitFeedback: vi.fn().mockResolvedValue(undefined),
  getSelectableModels: vi.fn().mockResolvedValue([]),
};

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <ChatProvider
      adapter={NOOP_ADAPTER}
      t={chatT}
      projectId="p1"
      config={null}
    >
      {children}
    </ChatProvider>
  );
}

function makeTurn(overrides: Partial<TurnResponse> = {}): TurnResponse {
  return {
    id: "turn-1",
    conversation_id: "conv-1",
    turn_index: 0,
    user_message: "test",
    answer_text: "answer",
    status: "ok",
    latency_ms: null,
    thought_summary: null,
    semantic_query: null,
    routed_sql: null,
    route: null,
    citations: null,
    user_feedback: null,
    judge_verdict: null,
    judge_reasoning: null,
    judge_metrics: null,
    guardrail_actions: null,
    usage_input_tokens: null,
    usage_output_tokens: null,
    rendered_output: null,
    llm_plan: null,
    query_result_rows: null,
    query_result_sample: null,
    calculation_steps: null,
    chart_type: null,
    provider: null,
    judge_pending: undefined,
    ...overrides,
  };
}

const REAL_METRICS: Record<string, number> = {
  "Factual accuracy": 1.0,
  "Query correctness": 1.0,
  Completeness: 0.6,
  Presentation: 0.8,
  "Narration rules": 1.0,
  Compliance: 1.0,
};

describe("JudgeVerdictStrip (F-025-05 render)", () => {
  it("renders nothing when no verdict and not pending", () => {
    const { container } = render(
      <Wrapper>
        <JudgeVerdictStrip turn={makeTurn()} />
      </Wrapper>,
    );
    expect(container.innerHTML).toBe("");
  });

  it("shows pending spinner when judge_pending is true", () => {
    render(
      <Wrapper>
        <JudgeVerdictStrip turn={makeTurn({ judge_pending: true })} />
      </Wrapper>,
    );
    expect(screen.getByText(chatT("judge.evaluating"))).toBeDefined();
  });

  it("renders the warn verdict with reasoning and real metric scores", () => {
    render(
      <Wrapper>
        <JudgeVerdictStrip
          turn={makeTurn({
            judge_verdict: "warn",
            judge_reasoning: "GB and US numbers match but DE and FR were omitted.",
            judge_metrics: REAL_METRICS,
          })}
        />
      </Wrapper>,
    );

    expect(screen.getByText(chatT("judge.concerned"))).toBeDefined();

    // Expand details
    fireEvent.click(screen.getByText(chatT("judge.concerned")));

    // Reasoning is the judge's text
    expect(screen.getByText(/GB and US numbers match/)).toBeDefined();
    // Metrics render with section titles and 0-5 scale (Math.round(v*5)/5)
    expect(screen.getByText(/Factual accuracy/)).toBeDefined();
    // 0.6 Completeness => Math.round(0.6*5) = 3 => "3/5"
    expect(screen.getByText(/3\/5/)).toBeDefined();
    // The old broken card text must not appear
    expect(screen.queryByText(/Confidence:/)).toBeNull();
    expect(screen.queryByText(/Rubric:/)).toBeNull();
  });

  it("renders fail and pass verdicts distinctly", () => {
    const { rerender } = render(
      <Wrapper>
        <JudgeVerdictStrip
          turn={makeTurn({
            judge_verdict: "fail",
            judge_reasoning: "Factual mismatch.",
            judge_metrics: { "Factual accuracy": 0.0 },
          })}
        />
      </Wrapper>,
    );
    expect(screen.getByText(chatT("judge.didNotApprove"))).toBeDefined();

    rerender(
      <Wrapper>
        <JudgeVerdictStrip
          turn={makeTurn({
            judge_verdict: "pass",
            judge_reasoning: "All good.",
            judge_metrics: { "Factual accuracy": 1.0 },
          })}
        />
      </Wrapper>,
    );
    expect(screen.getByText(chatT("judge.approves"))).toBeDefined();
  });
});
