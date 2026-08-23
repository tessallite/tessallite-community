import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
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
const setPendingPersonaIdMock = vi.fn();

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
  ChatCanvas: ({ disabled }: { disabled?: boolean }) => (
    <>
      <div data-testid="chat-canvas" data-disabled={String(Boolean(disabled))}>
        ChatCanvas
      </div>
      <button type="button" disabled={disabled}>Send message</button>
    </>
  ),
  TraceDrawer: () => null,
  useConversationStore: vi.fn((selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      activeConversationId: null,
      setActiveConversation: setActiveConversationMock,
      pendingPersonaId: null,
      setPendingPersonaId: setPendingPersonaIdMock,
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
          pendingPersonaId: null,
          setPendingPersonaId: setPendingPersonaIdMock,
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
          pendingPersonaId: null,
          setPendingPersonaId: setPendingPersonaIdMock,
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
      expect(createConversationMock).toHaveBeenCalledWith("proj-1", {
        persona_id: null,
      });
    });
  });

  it("L13-9196-SPA: sends the selected ProjectPersona on conversation create", async () => {
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([]);
    listPersonasMock.mockResolvedValue([
      {
        id: "project-persona-1",
        project_id: "proj-1",
        name: "Finance analyst",
        slug: "finance-analyst",
        description: null,
        model_scopes: [],
      },
    ]);
    createConversationMock.mockResolvedValue({
      id: "conv-project-persona",
      project_id: "proj-1",
      title: null,
      pinned_at: null,
      started_at: "2026-06-20T00:00:00Z",
      last_active_at: "2026-06-20T00:00:00Z",
      deleted_at: null,
      persona_id: "project-persona-1",
    });

    renderChat();
    const picker = await screen.findByRole("combobox", {
      name: "Project persona",
    });
    await userEvent.click(picker);
    await userEvent.click(await screen.findByRole("option", { name: "Finance analyst" }));

    expect(setPendingPersonaIdMock).toHaveBeenCalledWith("project-persona-1");
    await userEvent.click(screen.getByRole("button", { name: /new/i }));

    await waitFor(() => {
      expect(createConversationMock).toHaveBeenCalledWith("proj-1", {
        persona_id: "project-persona-1",
      });
    });
  });

  it("L13-R1-F5 keeps persisted ProjectPersona authority visible and blocks send while saving", async () => {
    const storeState = {
      activeConversationId: "conv-1",
      setActiveConversation: setActiveConversationMock,
      pendingPersonaId: null,
      setPendingPersonaId: setPendingPersonaIdMock,
    };
    vi.mocked(useConversationStore).mockImplementation(
      (selector: (s: Record<string, unknown>) => unknown) =>
        selector(storeState) as never,
    );
    getConfigMock.mockResolvedValue({ enabled: true });
    const conversation = {
      id: "conv-1",
      project_id: "proj-1",
      title: "Active chat",
      pinned_at: null,
      started_at: "2026-05-10T00:00:00Z",
      last_active_at: "2026-05-10T00:00:00Z",
      persona_id: null,
    };
    listConversationsMock.mockResolvedValue([conversation]);
    listPersonasMock.mockResolvedValue([
      {
        id: "project-persona-1",
        project_id: "proj-1",
        name: "Finance analyst",
        slug: "finance-analyst",
        description: null,
        model_scopes: [],
      },
    ]);
    let resolvePatch!: (value: typeof conversation) => void;
    patchConversationMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePatch = resolve;
      }),
    );

    renderChat();
    const picker = await screen.findByRole("combobox", {
      name: "Project persona",
    });
    await userEvent.click(picker);
    await userEvent.click(await screen.findByRole("option", { name: "Finance analyst" }));

    await waitFor(() => {
      expect(patchConversationMock).toHaveBeenCalledWith(
        "proj-1",
        "conv-1",
        { persona_id: "project-persona-1" },
      );
      expect(screen.getByRole("combobox", { name: "Project persona" })).not.toHaveTextContent(
        "Finance analyst",
      );
      expect(screen.getByTestId("chat-canvas")).toHaveAttribute(
        "data-disabled",
        "true",
      );
    });

    await act(async () => {
      resolvePatch({ ...conversation, persona_id: "project-persona-1" });
    });
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Project persona" })).toHaveTextContent(
        "Finance analyst",
      );
      expect(screen.getByTestId("chat-canvas")).toHaveAttribute(
        "data-disabled",
        "false",
      );
    });
  });

  it("L13 F5 restores the persisted ProjectPersona and reports a failed PATCH", async () => {
    vi.mocked(useConversationStore).mockImplementation(
      (selector: (s: Record<string, unknown>) => unknown) =>
        selector({
          activeConversationId: "conv-1",
          setActiveConversation: setActiveConversationMock,
          pendingPersonaId: null,
          setPendingPersonaId: setPendingPersonaIdMock,
        }) as never,
    );
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([{
      id: "conv-1", project_id: "proj-1", title: "Active", pinned_at: null,
      started_at: "2026-05-10T00:00:00Z", last_active_at: "2026-05-10T00:00:00Z",
      deleted_at: null, persona_id: null,
    }]);
    listPersonasMock.mockResolvedValue([{
      id: "project-persona-1", project_id: "proj-1", name: "Finance analyst",
      slug: "finance-analyst", description: null, model_scopes: [],
    }]);
    patchConversationMock.mockRejectedValueOnce(new Error("persist failed"));
    renderChat();
    const picker = await screen.findByRole("combobox", { name: "Project persona" });
    await userEvent.click(picker);
    await userEvent.click(await screen.findByRole("option", { name: "Finance analyst" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/previous selection was restored/i));
    expect(setPendingPersonaIdMock).toHaveBeenLastCalledWith(null);
  });

  it("L13-R1-F5 blocks direct New/create/send across a ProjectPersona transition", async () => {
    const storeState = {
      activeConversationId: "conv-1",
      setActiveConversation: setActiveConversationMock,
      pendingPersonaId: null,
      setPendingPersonaId: setPendingPersonaIdMock,
    };
    vi.mocked(useConversationStore).mockImplementation(
      (selector: (s: Record<string, unknown>) => unknown) =>
        selector(storeState) as never,
    );
    getConfigMock.mockResolvedValue({ enabled: true });
    listConversationsMock.mockResolvedValue([{
      id: "conv-1", project_id: "proj-1", title: "Active", pinned_at: null,
      started_at: "2026-05-10T00:00:00Z", last_active_at: "2026-05-10T00:00:00Z",
      deleted_at: null, persona_id: null,
    }]);
    listPersonasMock.mockResolvedValue([{
      id: "project-persona-1", project_id: "proj-1", name: "Finance analyst",
      slug: "finance-analyst", description: null, model_scopes: [],
    }]);
    let resolvePatch!: (value: unknown) => void;
    patchConversationMock.mockReturnValueOnce(new Promise((resolve) => {
      resolvePatch = resolve;
    }));

    renderChat();
    const picker = await screen.findByRole("combobox", { name: "Project persona" });
    await userEvent.click(picker);
    await userEvent.click(await screen.findByRole("option", { name: "Finance analyst" }));
    await waitFor(() => {
      expect(patchConversationMock).toHaveBeenCalledWith(
        "proj-1", "conv-1", { persona_id: "project-persona-1" },
      );
      expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
      expect(screen.getByTestId("chat-canvas")).toHaveAttribute("data-disabled", "true");
    });

    const newButton = screen.getByRole("button", { name: /new/i });
    expect(newButton).toBeDisabled();
    expect(createConversationMock).not.toHaveBeenCalled();

    await act(async () => {
      resolvePatch({
        id: "conv-1", project_id: "proj-1", title: "Active", pinned_at: null,
        started_at: "2026-05-10T00:00:00Z", last_active_at: "2026-05-10T00:00:00Z",
        deleted_at: null, persona_id: "project-persona-1",
      });
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Send message" })).not.toBeDisabled();
      expect(screen.getByRole("button", { name: /new/i })).not.toBeDisabled();
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

  it("threads a per-send Idempotency-Key to streamMessageRaw (Bug-6521)", async () => {
    const sse = createSseController();
    const stream = vi.fn().mockResolvedValue(sse.response);
    const adapter = makeAdapter({
      getTurns: vi.fn().mockResolvedValue([]),
      streamMessageRaw: stream,
    });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "Show revenue trend");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(1);
    });
    // 5th positional arg is the idempotency key.
    const key = stream.mock.calls[0][4];
    expect(typeof key).toBe("string");
    expect((key as string).length).toBeGreaterThan(0);

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

  it("reconciles a persisted turn on stream onError instead of showing error + retry (Bug-8336)", async () => {
    // Stream closes WITHOUT a terminal event -> messagesStream raises onError,
    // but the server DID persist the turn. getTurns returns empty on the
    // initial load, then the persisted turn once ChatCanvas refetches from
    // onError. The user must see the answer, NOT an error/retry that would
    // re-ask the already-answered question.
    const persistedTurn = makeTurn({
      id: "turn-persisted",
      user_message: "What is revenue?",
      answer_text: "Revenue was 1200.",
    });
    const sse = createSseController();
    const getTurns = vi
      .fn()
      .mockResolvedValueOnce([]) // initial load
      .mockResolvedValue([persistedTurn]); // onError reconcile refetch
    const stream = vi.fn().mockResolvedValue(sse.response);
    const adapter = makeAdapter({
      getTurns,
      streamMessageRaw: stream,
    });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "What is revenue?");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(1);
    });

    // Deliver content then close with NO terminal event -> onError path. Note we
    // do NOT emit a turn_id on turn.started (the production server does not
    // supply one), so this exercises the real reconcile path: a NEW terminal
    // turn (not in the loaded-empty prior snapshot) whose text matches the send.
    act(() => {
      sse.emit("narration.delta", { text: "Revenue was 1200." });
      sse.close();
    });

    // The persisted answer is reconciled onto the screen.
    await waitFor(() => {
      expect(screen.getByText("Revenue was 1200.")).toBeTruthy();
    });

    // No error toast and no retry button are offered. Bug-8370 — the toast
    // now resolves through the typed StreamErrorCode -> friendly i18n
    // message map (never the messagesStream.ts raw engine text), so assert
    // against the current friendly copy rather than a stale literal.
    expect(
      screen.queryByText(sharedChatMessages["stream.error.unexpectedEnd"]),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Retry sending message" }),
    ).toBeNull();

    // A follow-up refetch was made (reconcile), and no duplicate send occurred.
    expect(getTurns.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(stream).toHaveBeenCalledTimes(1);
  });

  it("shows error + retry on stream onError when no turn was persisted (Bug-8336)", async () => {
    // Stream closes with no terminal event and getTurns stays empty -> the
    // request genuinely failed, so the error toast and retry must appear, and a
    // retry issues a fresh send (new logical send).
    const sse = createSseController();
    const getTurns = vi.fn().mockResolvedValue([]);
    const stream = vi
      .fn()
      .mockResolvedValueOnce(sse.response)
      .mockResolvedValue(new Response(null, { status: 200 }));
    const adapter = makeAdapter({
      getTurns,
      streamMessageRaw: stream,
    });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "What is revenue?");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(1);
    });

    act(() => {
      sse.close(); // EOF, no terminal event, nothing persisted
    });

    // Error surfaces and the message is kept editable with a retry button.
    await waitFor(() => {
      expect(screen.getByLabelText("Message input")).toHaveValue(
        "What is revenue?",
      );
    });
    // Bug-8370 — the toast shows the typed StreamErrorCode's friendly i18n
    // message (a real body stream closing with no frames classifies as
    // 'unexpected_end'), never messagesStream.ts's raw engine text.
    expect(
      screen.getByText(sharedChatMessages["stream.error.unexpectedEnd"]),
    ).toBeTruthy();
    const retryBtn = await screen.findByRole("button", {
      name: "Retry sending message",
    });

    await userEvent.click(retryBtn);
    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(2);
    });
  });

  it("does NOT reconcile against a non-terminal streaming placeholder row (Bug-8336)", async () => {
    // The server commits a status="streaming" reservation placeholder with the
    // real user_message BEFORE the pipeline runs, and list_turns returns it
    // unfiltered. If the stream dies (orphaned reservation) the refetch finds
    // that placeholder — but it has no answer, so reconciling against it would
    // suppress error+retry and strand the user. onError must fall through to
    // error+retry, not treat the placeholder as a success.
    const placeholder = makeTurn({
      id: "turn-placeholder",
      user_message: "What is revenue?",
      answer_text: null,
      status: "streaming",
    });
    const sse = createSseController();
    const getTurns = vi
      .fn()
      .mockResolvedValueOnce([]) // initial load
      .mockResolvedValue([placeholder]); // onError reconcile refetch
    const stream = vi
      .fn()
      .mockResolvedValueOnce(sse.response)
      .mockResolvedValue(new Response(null, { status: 200 }));
    const adapter = makeAdapter({ getTurns, streamMessageRaw: stream });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "What is revenue?");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(1);
    });

    act(() => {
      sse.close(); // orphaned reservation: placeholder persisted, no answer
    });

    // Error + retry must appear despite the placeholder row existing.
    const retryBtn = await screen.findByRole("button", {
      name: "Retry sending message",
    });
    expect(screen.getByLabelText("Message input")).toHaveValue(
      "What is revenue?",
    );
    await userEvent.click(retryBtn);
    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(2);
    });
  });

  it("does NOT reconcile a failed new send against an older identical-text turn (Bug-8336)", async () => {
    // Re-asking the same question is normal. An OLD completed turn with the same
    // text must not make a genuinely-failed new send look successful. Here the
    // stream dies before turn.started (no server id for this send), so
    // reconciliation falls back to newness+text — and the only matching turn is
    // the pre-existing one, so it must NOT reconcile.
    const olderTurn = makeTurn({
      id: "turn-older",
      user_message: "What is revenue?",
      answer_text: "Revenue was 1000 last time.",
      status: "ok",
    });
    const sse = createSseController();
    const getTurns = vi.fn().mockResolvedValue([olderTurn]); // same before & after
    const stream = vi
      .fn()
      .mockResolvedValueOnce(sse.response)
      .mockResolvedValue(new Response(null, { status: 200 }));
    const adapter = makeAdapter({ getTurns, streamMessageRaw: stream });

    // The describe-block beforeEach sets activeConversationId="conv-1", so the
    // older turn is the initial load (a pre-existing turn for this send).
    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "What is revenue?");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(1);
    });

    act(() => {
      sse.close(); // dies before turn.started -> no server id for this send
    });

    // The failed new send must surface error + retry, not silently vanish.
    const retryBtn = await screen.findByRole("button", {
      name: "Retry sending message",
    });
    expect(screen.getByLabelText("Message input")).toHaveValue(
      "What is revenue?",
    );
    await userEvent.click(retryBtn);
    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(2);
    });
  });

  it("declines to reconcile when the prior-turns cache is unloaded, even on identical text (Bug-8336)", async () => {
    // Guards the R2-F2 unloaded-cache branch: if the send fires while the turns
    // query is still in flight (e.g. right after switching conversations), the
    // snapshot is `undefined`. The text fallback must NOT fire — otherwise an
    // OLDER identical-text turn revealed by the reconcile refetch would falsely
    // reconcile a genuinely failed new send. The initial getTurns never
    // resolves (cache stays undefined at send time); the reconcile refetch
    // returns a pre-existing identical-text turn. Error + retry must appear.
    const olderTurn = makeTurn({
      id: "turn-older",
      user_message: "What is revenue?",
      answer_text: "Revenue was 1000 last time.",
      status: "ok",
    });
    const sse = createSseController();
    // Initial load rejects (retry:false in the test QueryClient) so the query
    // settles with data === undefined — the cache is UNLOADED at send time. The
    // reconcile refetch then resolves the pre-existing identical-text turn.
    const getTurns = vi
      .fn()
      .mockRejectedValueOnce(new Error("turns load failed"))
      .mockResolvedValue([olderTurn]); // reconcile refetch
    const stream = vi
      .fn()
      .mockResolvedValueOnce(sse.response)
      .mockResolvedValue(new Response(null, { status: 200 }));
    const adapter = makeAdapter({ getTurns, streamMessageRaw: stream });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "What is revenue?");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(1);
    });

    act(() => {
      sse.close(); // dies before turn.started; cache was never loaded
    });

    // Must decline reconcile -> error + retry, not silently drop the send.
    const retryBtn = await screen.findByRole("button", {
      name: "Retry sending message",
    });
    expect(screen.getByLabelText("Message input")).toHaveValue(
      "What is revenue?",
    );
    await userEvent.click(retryBtn);
    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(2);
    });
  });

  it("reconciles a persisted clarify turn on onError (Bug-8336)", async () => {
    // `clarify` is a fully-releasable terminal outcome. If the stream drops
    // after a clarify turn persists, the user must see the clarifying question,
    // NOT an error + duplicate-risk retry.
    const clarifyTurn = makeTurn({
      id: "turn-clarify",
      user_message: "Show me sales",
      answer_text: "Which region did you mean?",
      status: "clarify",
    });
    const sse = createSseController();
    const getTurns = vi
      .fn()
      .mockResolvedValueOnce([]) // initial load (loaded-empty)
      .mockResolvedValue([clarifyTurn]); // onError reconcile refetch
    const stream = vi.fn().mockResolvedValue(sse.response);
    const adapter = makeAdapter({ getTurns, streamMessageRaw: stream });

    renderSharedUi(<RealChatCanvas />, adapter);

    const input = await screen.findByLabelText("Message input");
    await userEvent.type(input, "Show me sales");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => {
      expect(stream).toHaveBeenCalledTimes(1);
    });

    act(() => {
      sse.close(); // no terminal event, but the clarify turn was persisted
    });

    // The clarifying question is reconciled onto the screen; no error/retry.
    await waitFor(() => {
      expect(screen.getByText("Which region did you mean?")).toBeTruthy();
    });
    expect(
      screen.queryByRole("button", { name: "Retry sending message" }),
    ).toBeNull();
    expect(stream).toHaveBeenCalledTimes(1);
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

  // Bug-5960: the blocked/refused detail disclosure was a mouse-only
  // clickable Box with no accessible button semantics — keyboard-only and
  // screen-reader users could not discover or operate it.
  it("exposes the blocked/refused detail disclosure as an accessible, keyboard-operable button", async () => {
    renderSharedUi(
      <RealAssistantTurn
        turn={makeTurn({
          status: "refused",
          answer_text: "I cannot help with that request.",
        })}
      />,
    );

    const disclosure = screen.getByRole("button", {
      name: "This request was refused",
    });
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    const controlsId = disclosure.getAttribute("aria-controls");
    expect(controlsId).toBeTruthy();
    expect(document.getElementById(controlsId!)).toBeTruthy();

    disclosure.focus();
    expect(disclosure).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(disclosure).toHaveAttribute("aria-expanded", "false");

    await userEvent.keyboard(" ");
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
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
    // Bug-6584: the iframe must NOT emit a hardcoded /charts.min.css <link>.
    // Chart styling is supplied inline via the chartsCss prop; a served-URL
    // <link> only appears when a caller explicitly opts in via chartsCssHref
    // (this render passes neither), so the Excel task-pane host cannot 404.
    expect(renderedFrame.getAttribute("srcdoc")).not.toContain(
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
              definition: null,
              route_type: null,
              filter_grain: null,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText("Transaction Count: 100,000")).toBeTruthy();
    expect(screen.queryByText(/1\.0E\+5/)).toBeNull();
  });

  describe("citation provenance dialog (Bug-8181)", () => {
    function twoCitationTurn() {
      return makeTurn({
        citations: [
          {
            kind: "measure",
            id: "revenue",
            name: "revenue",
            display_name: "Revenue",
            value: 4200000,
            definition: "Total gross revenue recognised in the period.",
            route_type: "aggregate",
            filter_grain: "Filtered by country = US · Grouped by business_month",
          },
          {
            kind: "dimension",
            id: "country",
            name: "country",
            display_name: "Country",
            value: null,
            definition: null,
            route_type: "aggregate",
            filter_grain: null,
          },
        ],
      });
    }

    it("opens a citation's OWN provenance dialog on click, not the generic trace drawer", async () => {
      const onOpenTrace = vi.fn();
      renderSharedUi(
        <RealAssistantTurn turn={twoCitationTurn()} onOpenTrace={onOpenTrace} />,
      );

      await userEvent.click(screen.getByText("Revenue: 4,200,000"));

      // The provenance dialog opened with THIS citation's data...
      const dialog = within(screen.getByRole("dialog"));
      expect(dialog.getByText("How this number was calculated")).toBeTruthy();
      expect(
        dialog.getByText("Total gross revenue recognised in the period."),
      ).toBeTruthy();
      expect(
        dialog.getByText("Filtered by country = US · Grouped by business_month"),
      ).toBeTruthy();
      expect(dialog.getByText("Route: aggregate")).toBeTruthy();
      // ...and clicking a chip never fires the generic trace drawer directly
      // (that is now an opt-in secondary action from inside the dialog).
      expect(onOpenTrace).not.toHaveBeenCalled();
    });

    it("shows the OTHER citation's data when a different chip is clicked", async () => {
      renderSharedUi(<RealAssistantTurn turn={twoCitationTurn()} />);

      await userEvent.click(screen.getByText("Country"));

      const dialog = within(screen.getByRole("dialog"));
      expect(dialog.getByText("Dimension: Country")).toBeTruthy();
      expect(
        dialog.getByText("No definition recorded for this field."),
      ).toBeTruthy();
      expect(
        dialog.getByText("No filters. This is the unfiltered total."),
      ).toBeTruthy();
    });

    it("'View trace' closes the dialog and invokes the host's onOpenTrace", async () => {
      const onOpenTrace = vi.fn();
      renderSharedUi(
        <RealAssistantTurn turn={twoCitationTurn()} onOpenTrace={onOpenTrace} />,
      );

      await userEvent.click(screen.getByText("Revenue: 4,200,000"));
      expect(screen.getByRole("dialog")).toBeTruthy();

      await userEvent.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: "View trace",
        }),
      );

      expect(onOpenTrace).toHaveBeenCalledTimes(1);
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).toBeNull();
      });
    });

    it("omits the 'View trace' action when the host does not wire onOpenTrace", async () => {
      renderSharedUi(<RealAssistantTurn turn={twoCitationTurn()} />);

      await userEvent.click(screen.getByText("Revenue: 4,200,000"));

      const dialog = within(screen.getByRole("dialog"));
      expect(dialog.getByText("How this number was calculated")).toBeTruthy();
      expect(dialog.queryByRole("button", { name: "View trace" })).toBeNull();
    });

    it("closes on the dialog's own close button", async () => {
      renderSharedUi(<RealAssistantTurn turn={twoCitationTurn()} />);

      await userEvent.click(screen.getByText("Revenue: 4,200,000"));
      await userEvent.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: "Close",
        }),
      );

      await waitFor(() => {
        expect(screen.queryByRole("dialog")).toBeNull();
      });
    });
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
