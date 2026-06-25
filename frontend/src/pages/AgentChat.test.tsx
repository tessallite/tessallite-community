import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { Box } from "@mui/material";
import { ConfirmProvider } from "../components/Confirm";
import sharedChatMessages from "../i18n/en/shared-chat.json";
import { ChatProvider as RealChatProvider } from "../../../shared-ui/src/providers/ChatProvider";
import { ChatCanvas as RealChatCanvas } from "../../../shared-ui/src/components/ChatCanvas";
import { AssistantTurn as RealAssistantTurn } from "../../../shared-ui/src/components/AssistantTurn";
import { useConversationStore as useRealConversationStore } from "../../../shared-ui/src/stores/conversationStore";
import type { AgentChatAdapter } from "../../../shared-ui/src/types/adapter";
import type { TurnResponse } from "../../../shared-ui/src/types/turn";

const getConfigMock = vi.fn();
const listConversationsMock = vi.fn();
const listTurnsMock = vi.fn();
const listPersonasMock = vi.fn();
const createConversationMock = vi.fn();
const patchConversationMock = vi.fn();
const deleteConversationMock = vi.fn();
const setActiveConversationMock = vi.fn();

vi.mock("../api/agentApi", () => ({
  agentApi: {
    getConfig: (...args: unknown[]) => getConfigMock(...args),
    listConversations: (...args: unknown[]) => listConversationsMock(...args),
    listTurns: (...args: unknown[]) => listTurnsMock(...args),
    listPersonas: (...args: unknown[]) => listPersonasMock(...args),
    createConversation: (...args: unknown[]) => createConversationMock(...args),
    patchConversation: (...args: unknown[]) => patchConversationMock(...args),
    deleteConversation: (...args: unknown[]) => deleteConversationMock(...args),
    submitFeedback: vi.fn(),
  },
}));

vi.mock("../api/hooks", () => ({
  useProject: () => ({
    data: { id: "proj-1", display_name: "Test Project", slug: "test" },
    isLoading: false,
  }),
}));

vi.mock("@tessallite/shared-ui", () => ({
  ChatProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="chat-provider">{children}</div>
  ),
  ChatCanvas: () => <div data-testid="chat-canvas">ChatCanvas</div>,
  TraceDrawer: () => null,
  useConversationStore: vi.fn((selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      activeConversationId: null,
      setActiveConversation: setActiveConversationMock,
    }),
  ),
}));

import AgentChat from "./AgentChat";
import { useConversationStore } from "@tessallite/shared-ui";

const t = (key: string, vars?: Record<string, string | number>) => {
  let text =
    (sharedChatMessages as Record<string, string>)[key] ?? key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.replace(`{{${name}}}`, String(value));
    }
  }
  return text;
};

function makeTurn(overrides: Partial<TurnResponse> = {}): TurnResponse {
  return {
    id: "turn-1",
    conversation_id: "conv-1",
    turn_index: 1,
    user_message: "Show revenue",
    answer_text: "Revenue was 1200.",
    status: "ok",
    latency_ms: 120,
    thought_summary: null,
    semantic_query: null,
    routed_sql: null,
    route: "source",
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
    judge_pending: false,
    ...overrides,
  };
}

function makeAdapter(
  overrides: Partial<AgentChatAdapter> = {},
): AgentChatAdapter {
  return {
    getConversations: vi.fn().mockResolvedValue([]),
    createConversation: vi.fn().mockResolvedValue({
      id: "conv-1",
      project_id: "proj-1",
      title: null,
      pinned_at: null,
      started_at: "2026-06-21T00:00:00Z",
      last_active_at: "2026-06-21T00:00:00Z",
      deleted_at: null,
      persona_id: null,
      pinned_model_id: null,
    }),
    getConversation: vi.fn().mockResolvedValue({
      id: "conv-1",
      project_id: "proj-1",
      title: "Conversation",
      pinned_at: null,
      started_at: "2026-06-21T00:00:00Z",
      last_active_at: "2026-06-21T00:00:00Z",
      deleted_at: null,
      persona_id: null,
      pinned_model_id: null,
    }),
    updateConversation: vi.fn().mockResolvedValue({
      id: "conv-1",
      project_id: "proj-1",
      title: "Conversation",
      pinned_at: null,
      started_at: "2026-06-21T00:00:00Z",
      last_active_at: "2026-06-21T00:00:00Z",
      deleted_at: null,
      persona_id: null,
      pinned_model_id: null,
    }),
    deleteConversation: vi.fn().mockResolvedValue(undefined),
    getTurns: vi.fn().mockResolvedValue([]),
    streamMessageRaw: vi.fn().mockResolvedValue(new Response(null)),
    submitFeedback: vi.fn().mockResolvedValue(undefined),
    getSelectableModels: vi.fn().mockResolvedValue([]),
    getConfig: vi.fn().mockResolvedValue(null),
    getPersonas: vi.fn().mockResolvedValue([]),
    ...overrides,
  };
}

function renderSharedUi(
  children: React.ReactNode,
  adapter: AgentChatAdapter = makeAdapter(),
) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <RealChatProvider
        adapter={adapter}
        t={t}
        projectId="proj-1"
        config={null}
      >
        {children}
      </RealChatProvider>
    </QueryClientProvider>,
  );
}

function createSseController() {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
    emit(event: string, data: Record<string, unknown>) {
      controller?.enqueue(
        encoder.encode(
          `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
        ),
      );
    },
    close() {
      controller?.close();
    },
  };
}

function renderChat(initialEntry = "/t/tenant-1/p/proj-1/agent") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <ConfirmProvider>
          <MemoryRouter
            initialEntries={[initialEntry]}
            future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
          >
            <Routes>
              <Route
                path="/t/:tenantId/p/:projectId/agent"
                element={<AgentChat />}
              />
            </Routes>
          </MemoryRouter>
        </ConfirmProvider>
      </QueryClientProvider>,
    ),
  };
}

describe("AgentChat page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useConversationStore).mockImplementation(
      (selector: (s: Record<string, unknown>) => unknown) =>
        selector({
          activeConversationId: null,
          setActiveConversation: setActiveConversationMock,
        }) as never,
    );
    listTurnsMock.mockResolvedValue([]);
    listPersonasMock.mockResolvedValue([]);
    patchConversationMock.mockResolvedValue({});
    deleteConversationMock.mockResolvedValue({});
  });

  it("shows loading spinner while config loads", () => {
    getConfigMock.mockReturnValue(new Promise(() => {}));
    renderChat();
    expect(screen.getByRole("progressbar")).toBeTruthy();
  });

  it("shows not-enabled alert when agent is disabled", async () => {
    getConfigMock.mockResolvedValue({ enabled: false });
    renderChat();
    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
  });

  it("renders conversation list when agent is enabled", async () => {
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([
      {
        id: "conv-1",
        project_id: "proj-1",
        title: "First chat",
        pinned_at: null,
        started_at: "2026-05-10T00:00:00Z",
        last_active_at: "2026-05-10T00:00:00Z",
        deleted_at: null,
        persona_id: null,
      },
    ]);
    renderChat();
    await waitFor(() => {
      expect(screen.getByText("First chat")).toBeTruthy();
    });
  });

  it("shows placeholder when no conversation is selected", async () => {
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([]);
    renderChat();
    await waitFor(() => {
      expect(
        screen.getByText(/select.*conversation|create/i),
      ).toBeTruthy();
    });
  });

  it("renders ChatCanvas inside ChatProvider when a conversation is active", async () => {
    vi.mocked(useConversationStore).mockImplementation(
      (selector: (s: Record<string, unknown>) => unknown) =>
        selector({
          activeConversationId: "conv-1",
          setActiveConversation: setActiveConversationMock,
        }) as never,
    );

    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([
      {
        id: "conv-1",
        project_id: "proj-1",
        title: "Active chat",
        pinned_at: null,
        started_at: "2026-05-10T00:00:00Z",
        last_active_at: "2026-05-10T00:00:00Z",
        deleted_at: null,
        persona_id: null,
      },
    ]);

    renderChat();
    await waitFor(() => {
      expect(screen.getByTestId("chat-canvas")).toBeTruthy();
    });
  });

  it("creates a new conversation when the add button is clicked", async () => {
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([]);
    createConversationMock.mockResolvedValue({
      id: "conv-new",
      project_id: "proj-1",
      title: null,
      pinned_at: null,
      started_at: "2026-06-20T00:00:00Z",
      last_active_at: "2026-06-20T00:00:00Z",
      deleted_at: null,
      persona_id: null,
    });

    renderChat();
    await waitFor(() => {
      expect(listConversationsMock).toHaveBeenCalled();
    });

    const addButton = screen.getByRole("button", { name: /new/i });
    await userEvent.click(addButton);

    await waitFor(() => {
      expect(createConversationMock).toHaveBeenCalledWith("proj-1");
    });
  });

  it("activates a conversation from the deep-link query parameter", async () => {
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([
      {
        id: "conv-deep-link",
        project_id: "proj-1",
        title: "Deep linked chat",
        pinned_at: null,
        started_at: "2026-06-21T00:00:00Z",
        last_active_at: "2026-06-21T00:00:00Z",
        deleted_at: null,
        persona_id: null,
      },
    ]);

    renderChat("/t/tenant-1/p/proj-1/agent?conversation=conv-deep-link");

    await waitFor(() => {
      expect(setActiveConversationMock).toHaveBeenCalledWith(
        "conv-deep-link",
      );
    });
  });

  it("filters conversations by search term", async () => {
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([
      {
        id: "conv-1",
        project_id: "proj-1",
        title: "Revenue analysis",
        pinned_at: null,
        started_at: "2026-05-10T00:00:00Z",
        last_active_at: "2026-05-10T00:00:00Z",
        deleted_at: null,
        persona_id: null,
      },
      {
        id: "conv-2",
        project_id: "proj-1",
        title: "Cost breakdown",
        pinned_at: null,
        started_at: "2026-05-09T00:00:00Z",
        last_active_at: "2026-05-09T00:00:00Z",
        deleted_at: null,
        persona_id: null,
      },
    ]);

    renderChat();
    await waitFor(() => {
      expect(screen.getByText("Revenue analysis")).toBeTruthy();
      expect(screen.getByText("Cost breakdown")).toBeTruthy();
    });

    const searchInput = screen.getByPlaceholderText(/search/i);
    await userEvent.type(searchInput, "Revenue");

    expect(screen.getByText("Revenue analysis")).toBeTruthy();
    expect(screen.queryByText("Cost breakdown")).toBeNull();
  });

  it("displays project name in header", async () => {
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([]);
    renderChat();
    await waitFor(() => {
      expect(screen.getByText(/Test Project/)).toBeTruthy();
    });
  });
});

describe("AgentChat shared chat interaction states", () => {
  beforeEach(() => {
    useRealConversationStore.setState({
      activeConversationId: "conv-1",
      draftTitleCandidate: null,
      isStreaming: false,
      pendingModelId: null,
      pendingPersonaId: null,
    });
  });

  it("streams thoughts incrementally with compact formatted text", async () => {
    const sse = createSseController();
    const adapter = makeAdapter({
      getTurns: vi.fn().mockResolvedValue([]),
      streamMessageRaw: vi.fn().mockResolvedValue(sse.response),
    });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "Show revenue trend");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(adapter.streamMessageRaw).toHaveBeenCalledTimes(1);
    });

    act(() => {
      sse.emit("thought.delta", { text: "**Look" });
    });
    await waitFor(() => {
      expect(screen.getByText("Thought stream is available")).toBeTruthy();
    });
    await userEvent.click(
      screen.getByRole("button", { name: "Expand thought stream" }),
    );
    await waitFor(() => {
      expect(screen.getByTestId("streaming-thought")).toHaveTextContent(
        "Look",
      );
    });
    expect(screen.queryByText(/\*\*/)).toBeNull();

    act(() => {
      sse.emit("thought.delta", { text: " up\\nmonthly data" });
    });
    await waitFor(() => {
      expect(screen.getByTestId("streaming-thought")).toHaveTextContent(
        /Look up\s+monthly data/,
      );
    });
    expect(screen.queryByText(/\\n/)).toBeNull();

    act(() => {
      sse.emit("turn.completed", { turn_id: "turn-1" });
      sse.close();
    });
  });

  it("keeps failed sends editable and submits retry only once", async () => {
    const adapter = makeAdapter({
      getTurns: vi.fn().mockResolvedValue([]),
      streamMessageRaw: vi
        .fn()
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ error: "Network down" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          }),
        )
        .mockResolvedValueOnce(new Response(null, { status: 200 })),
    });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "Correct this question");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(screen.getByLabelText("Message input")).toHaveValue(
        "Correct this question",
      );
    });

    await userEvent.click(
      screen.getByRole("button", { name: "Retry sending message" }),
    );

    await waitFor(() => {
      expect(adapter.streamMessageRaw).toHaveBeenCalledTimes(2);
    });
  });

  it("does not expose blocked answer text or judge reasoning and retains safe table and trace actions", async () => {
    const onOpenTrace = vi.fn();
    const onRephrase = vi.fn();
    const onResend = vi.fn();

    renderSharedUi(
      <RealAssistantTurn
        turn={makeTurn({
          status: "judge_blocked",
          answer_text: "Unsafe blocked answer should stay hidden",
          judge_reasoning: "The answer was missing source evidence.",
          thought_summary: "**Checked model**\\n- compared totals",
          semantic_query: { measures: ["Revenue"] },
        })}
        resultRows={[{ sales_amount: 1200 }]}
        visibility={{
          showThoughtProcess: true,
          showSemanticQuery: true,
          showPhysicalQuery: false,
        }}
        onOpenTrace={onOpenTrace}
        onRephrase={onRephrase}
        onResend={onResend}
      />,
    );

    expect(
      screen.queryByText(/Unsafe blocked answer should stay hidden/i),
    ).toBeNull();
    expect(
      screen.queryByText("The answer was missing source evidence."),
    ).toBeNull();
    expect(
      screen.getByText("This response was blocked by the quality review"),
    ).toBeTruthy();
    expect(screen.getByText("Sales Amount")).toBeTruthy();
    expect(screen.getByText("1,200")).toBeTruthy();

    await userEvent.click(
      screen.getByRole("button", { name: "Retry with review note" }),
    );
    expect(onResend).toHaveBeenCalledWith(
      "Please correct the previous answer and answer again without using the blocked wording.",
    );

    await userEvent.click(screen.getByRole("button", { name: "View trace" }));
    expect(onOpenTrace).toHaveBeenCalledTimes(1);
  });

  it("shows refused answer text without treating it as hidden blocked content", () => {
    renderSharedUi(
      <RealAssistantTurn
        turn={makeTurn({
          status: "refused",
          answer_text: "I cannot help with that request.",
        })}
      />,
    );

    expect(screen.getByText("This request was refused")).toBeTruthy();
    expect(screen.getByText("I cannot help with that request.")).toBeTruthy();
    expect(
      screen.queryByText(/blocked response content is hidden/i),
    ).toBeNull();
  });

  it("localizes refused guardrail messages from i18n keys", () => {
    renderSharedUi(
      <RealAssistantTurn
        turn={makeTurn({
          status: "refused",
          answer_text: "raw_records_not_supported_by_aggregate_tools",
          guardrail_actions: [
            {
              reason: "raw_records_not_supported_by_aggregate_tools",
              message_i18n_key: "turn.rawRecordsNotSupported",
            },
          ],
        })}
      />,
    );

    expect(screen.getByText("This request was refused")).toBeTruthy();
    expect(
      screen.getByText(
        "Raw transaction records are not supported by the aggregate query tools. Ask for a count, total, or grouped summary instead.",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByText("raw_records_not_supported_by_aggregate_tools"),
    ).toBeNull();
  });

  it("renders backend shaped output ahead of sample-row chart inference", () => {
    const turn = makeTurn({
      rendered_output:
        '<div class="matrix-pivot-table">Backend stacked matrix output</div>',
      query_result_sample: [
        {
          country_code: "AE",
          payment_method: "BANK_TRANSFER",
          Revenue: "4395988.49",
        },
      ],
    });

    renderSharedUi(
      <RealAssistantTurn
        turn={turn}
        resultRows={turn.query_result_sample ?? undefined}
      />,
    );

    const renderedFrame = screen.getByTitle("Rendered answer output");
    expect(renderedFrame).toBeTruthy();
    expect(renderedFrame.getAttribute("srcdoc")).toContain(
      "Backend stacked matrix output",
    );
    expect(renderedFrame.getAttribute("srcdoc")).toContain(
      'href="/charts.min.css"',
    );
    expect(screen.getByText("0.1s")).toBeTruthy();
    expect(screen.queryByText("{{seconds}}s")).toBeNull();
    expect(screen.getByRole("button", { name: "Show data (1 rows)" })).toBeTruthy();
    expect(screen.queryByText("AE - BANK_TRANSFER")).toBeNull();
  });

  it("renders backend visual artifacts with shaped ECharts data and a separate table", async () => {
    const artifact = {
      kind: "tessallite.visual.v1",
      renderer: "echarts",
      chart_type: "bar",
      columns: ["country", "Revenue"],
      rows: [{ country: "France", Revenue: 1250 }],
      include_table: true,
      palette: "default",
      size: "md",
    };
    const turn = makeTurn({
      rendered_output: JSON.stringify(artifact),
      query_result_sample: [
        {
          country_code: "AE",
          payment_method: "BANK_TRANSFER",
          Revenue: "4395988.49",
        },
      ],
    });

    renderSharedUi(
      <RealAssistantTurn
        turn={turn}
        resultRows={turn.query_result_sample ?? undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "Visual" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Show data (1 rows)" }),
    ).toBeTruthy();

    await userEvent.click(
      screen.getByRole("button", { name: "Show data (1 rows)" }),
    );

    expect(screen.getByRole("cell", { name: "France" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: "1,250" })).toBeTruthy();
    expect(screen.queryByText("BANK_TRANSFER")).toBeNull();
    expect(screen.queryByTitle("Rendered answer output")).toBeNull();
  });

  it("formats scientific numeric strings in citation chips", () => {
    renderSharedUi(
      <RealAssistantTurn
        turn={makeTurn({
          citations: [
            {
              kind: "measure",
              id: "transaction_count",
              name: "transaction_count",
              display_name: "Transaction Count",
              value: "1.0E+5",
            },
          ],
        })}
      />,
    );

    expect(screen.getByText("Transaction Count: 100,000")).toBeTruthy();
    expect(screen.queryByText(/1\.0E\+5/)).toBeNull();
  });

  it("disables blocked recovery actions while a send is in flight", () => {
    renderSharedUi(
      <RealAssistantTurn
        turn={makeTurn({
          status: "judge_blocked",
          answer_text: "Hidden answer",
          judge_reasoning: "Needs correction.",
        })}
        onRephrase={vi.fn()}
        onResend={vi.fn()}
        actionsDisabled
      />,
    );

    expect(screen.getByRole("button", { name: "Rephrase" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Retry with review note" }),
    ).toBeDisabled();
  });

  it("renders answer, thought, chart data, and blocked states at desktop and narrow widths", () => {
    const turn = makeTurn({
      thought_summary: "## Plan\\n**Review trend**",
      query_result_sample: [
        { month: "2026-01", sales_amount: 100 },
        { month: "2026-02", sales_amount: 125 },
      ],
    });

    const { rerender } = renderSharedUi(
      <Box data-testid="desktop-layout" sx={{ width: 1180 }}>
        <RealAssistantTurn
          turn={turn}
          resultRows={turn.query_result_sample ?? undefined}
          visibility={{
            showThoughtProcess: true,
            showSemanticQuery: false,
            showPhysicalQuery: false,
          }}
        />
        <RealAssistantTurn
          turn={makeTurn({
            id: "turn-2",
            status: "judge_blocked",
            answer_text: "Hidden blocked content",
            judge_reasoning: "Needs review.",
          })}
          resultRows={[{ sales_amount: 999 }]}
        />
      </Box>,
    );

    expect(screen.getByText("Revenue was 1200.")).toBeTruthy();
    expect(screen.getByText(/Review trend/)).toBeTruthy();
    expect(screen.queryByText(/Hidden blocked content/)).toBeNull();
    expect(screen.getByTestId("desktop-layout")).toBeTruthy();

    rerender(
      <QueryClientProvider
        client={
          new QueryClient({
            defaultOptions: { queries: { retry: false } },
          })
        }
      >
        <RealChatProvider
          adapter={makeAdapter()}
          t={t}
          projectId="proj-1"
          config={null}
        >
          <Box data-testid="narrow-layout" sx={{ width: 375 }}>
            <RealAssistantTurn
              turn={turn}
              resultRows={turn.query_result_sample ?? undefined}
              visibility={{
                showThoughtProcess: true,
                showSemanticQuery: false,
                showPhysicalQuery: false,
              }}
            />
          </Box>
        </RealChatProvider>
      </QueryClientProvider>,
    );

    expect(screen.getByText(/Review trend/)).toBeTruthy();
    expect(screen.getByTestId("narrow-layout")).toBeTruthy();
  });
});
