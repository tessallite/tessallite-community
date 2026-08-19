import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import WebhookPanel from "./WebhookPanel";
import { DEFAULT_AGENT_CONFIG, type AgentConfig } from "../../api/agentApi";

/**
 * Bug-8411 — the agent webhook Settings surface.
 *
 * The endpoints (rotate-secret, DLQ list/retry/discard) and their API-client
 * wrappers existed for a whole phase with ZERO call sites anywhere in the
 * SPA, so the product shipped a webhook a modeller could configure but never
 * operate. These tests assert the user-visible outcome of each control, not
 * that a mock was called.
 */

const mocks = vi.hoisted(() => ({
  listWebhookEventTypes: vi.fn(),
  listWebhookDlq: vi.fn(),
  rotateWebhookSecret: vi.fn(),
  retryWebhookDlq: vi.fn(),
  discardWebhookDlq: vi.fn(),
}));

vi.mock("../../api/agentApi", async () => {
  const actual = await vi.importActual<typeof import("../../api/agentApi")>(
    "../../api/agentApi",
  );
  return { ...actual, agentApi: { ...actual.agentApi, ...mocks } };
});

const EVENT_TYPES = [
  { value: "conversation.started", label: "Conversation Started" },
  { value: "turn.completed", label: "Turn Completed" },
  { value: "turn.refused", label: "Turn Refused" },
  { value: "turn.judge_blocked", label: "Turn Blocked by Judge" },
  { value: "turn.feedback", label: "Turn Feedback Submitted" },
];

const DLQ_ROW = {
  id: "dlq-1",
  event_type: "turn.completed",
  target_host: "https://receiver.example",
  attempt_count: 4,
  last_status_code: 503,
  last_error: "HTTP 503: upstream unavailable",
  first_attempted_at: "2026-07-29T10:00:00Z",
  last_attempted_at: "2026-07-29T10:06:00Z",
  resolved_at: null,
  payload: {},
};

function renderPanel(
  overrides: Partial<AgentConfig> = {},
  webhookSecretRotated = false,
) {
  const update = vi.fn();
  const draft: AgentConfig = {
    ...DEFAULT_AGENT_CONFIG,
    webhook_url: "https://receiver.example/hooks/abc",
    ...overrides,
  };
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <WebhookPanel
        projectId="proj-1"
        draft={draft}
        update={update}
        webhookSecretRotated={webhookSecretRotated}
      />
    </QueryClientProvider>,
  );
  return { update };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listWebhookEventTypes.mockResolvedValue(EVENT_TYPES);
  mocks.listWebhookDlq.mockResolvedValue([]);
  mocks.rotateWebhookSecret.mockResolvedValue({ signing_secret: "s3cr3t-plaintext" });
  mocks.retryWebhookDlq.mockResolvedValue(undefined);
  mocks.discardWebhookDlq.mockResolvedValue(undefined);
});

describe("WebhookPanel — signing secret", () => {
  it("shows the Bug-8553 notice when the save response reports URL rotation", () => {
    renderPanel({}, true);

    expect(
      screen.getByText(/signing secret was rotated because the webhook URL changed/i),
    ).toBeInTheDocument();
  });

  it("does not show the Bug-8553 notice for an ordinary render", () => {
    renderPanel();

    expect(
      screen.queryByText(/signing secret was rotated because the webhook URL changed/i),
    ).not.toBeInTheDocument();
  });

  it("reveals the rotated secret once, with the copy-it-now warning", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole("button", { name: "Rotate signing secret" }));

    await waitFor(() =>
      expect(screen.getByDisplayValue("s3cr3t-plaintext")).toBeInTheDocument(),
    );
    expect(screen.getByText(/shown once and cannot be retrieved again/i))
      .toBeInTheDocument();
  });

  it("cannot rotate a secret before a webhook URL exists", () => {
    renderPanel({ webhook_url: null });
    expect(
      screen.getByRole("button", { name: "Rotate signing secret" }),
    ).toBeDisabled();
  });

  it("surfaces a rotate failure to the user instead of failing silently", async () => {
    const user = userEvent.setup();
    mocks.rotateWebhookSecret.mockRejectedValue({
      response: { data: { detail: "Agent not configured" } },
    });
    renderPanel();

    await user.click(screen.getByRole("button", { name: "Rotate signing secret" }));
    await waitFor(() =>
      expect(screen.getByText("Agent not configured")).toBeInTheDocument(),
    );
  });
});

describe("WebhookPanel — event subscription", () => {
  it("renders one checkbox per backend-published event, never a hard-coded list", async () => {
    renderPanel();
    await waitFor(() =>
      expect(screen.getByLabelText("Conversation started")).toBeInTheDocument(),
    );
    for (const label of [
      "Conversation started",
      "Answer delivered",
      "Question refused",
      "Answer blocked by the judge",
      "Feedback submitted",
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
  });

  it("treats a null filter list as 'all events', matching how the dispatcher reads it", async () => {
    renderPanel({ webhook_event_filters: null });
    await waitFor(() =>
      expect(
        screen.getByLabelText(/^All events/),
      ).toBeChecked(),
    );
  });

  it("expands 'all events' into every individual event rather than an empty list", async () => {
    // An empty subscription is rejected by the backend (it must never be read
    // as 'everything' -- Bug-7330), so unticking the wildcard must not leave
    // the user with a body the save will reject.
    const user = userEvent.setup();
    const { update } = renderPanel({ webhook_event_filters: ["*"] });
    await waitFor(() => expect(screen.getByLabelText("Answer delivered")).toBeInTheDocument());

    await user.click(screen.getByLabelText(/^All events/));

    expect(update).toHaveBeenCalledWith(
      "webhook_event_filters",
      EVENT_TYPES.map((e) => e.value),
    );
  });

  it("removes only the unticked event from an explicit subscription", async () => {
    const user = userEvent.setup();
    const { update } = renderPanel({
      webhook_event_filters: ["turn.completed", "turn.feedback"],
    });
    await waitFor(() => expect(screen.getByLabelText("Answer delivered")).toBeInTheDocument());

    await user.click(screen.getByLabelText("Answer delivered"));

    expect(update).toHaveBeenCalledWith("webhook_event_filters", ["turn.feedback"]);
  });

  it("warns when the subscription is empty instead of letting the save fail unexplained", async () => {
    renderPanel({ webhook_event_filters: [] });
    await waitFor(() =>
      expect(screen.getByText(/Select at least one event/i)).toBeInTheDocument(),
    );
  });
});

describe("WebhookPanel — dead-letter queue", () => {
  it("lists an undelivered event with its host, attempts and error", async () => {
    mocks.listWebhookDlq.mockResolvedValue([DLQ_ROW]);
    renderPanel();

    const row = await screen.findByRole("row", { name: /turn\.completed/ });
    expect(within(row).getByText("https://receiver.example")).toBeInTheDocument();
    expect(within(row).getByText("4")).toBeInTheDocument();
    expect(
      within(row).getByText(/503 — HTTP 503: upstream unavailable/),
    ).toBeInTheDocument();
  });

  it("retries one entry and refreshes the list", async () => {
    const user = userEvent.setup();
    mocks.listWebhookDlq.mockResolvedValue([DLQ_ROW]);
    renderPanel();

    const row = await screen.findByRole("row", { name: /turn\.completed/ });
    await user.click(within(row).getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(mocks.retryWebhookDlq).toHaveBeenCalledWith("proj-1", "dlq-1"),
    );
    await waitFor(() =>
      expect(mocks.listWebhookDlq.mock.calls.length).toBeGreaterThan(1),
    );
  });

  it("discards one entry and refreshes the list", async () => {
    const user = userEvent.setup();
    mocks.listWebhookDlq.mockResolvedValue([DLQ_ROW]);
    renderPanel();

    const row = await screen.findByRole("row", { name: /turn\.completed/ });
    await user.click(within(row).getByRole("button", { name: "Discard" }));

    await waitFor(() =>
      expect(mocks.discardWebhookDlq).toHaveBeenCalledWith("proj-1", "dlq-1"),
    );
  });

  it("surfaces a retry failure (e.g. the 409 already-resolved guard)", async () => {
    const user = userEvent.setup();
    mocks.listWebhookDlq.mockResolvedValue([DLQ_ROW]);
    mocks.retryWebhookDlq.mockRejectedValue({
      response: { data: { detail: "DLQ entry is already resolved" } },
    });
    renderPanel();

    const row = await screen.findByRole("row", { name: /turn\.completed/ });
    await user.click(within(row).getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.getByText("DLQ entry is already resolved")).toBeInTheDocument(),
    );
  });

  it("does not query the DLQ at all when no webhook URL is configured", async () => {
    renderPanel({ webhook_url: null });
    await waitFor(() =>
      expect(screen.getByText(/Set a webhook URL to start tracking/i))
        .toBeInTheDocument(),
    );
    expect(mocks.listWebhookDlq).not.toHaveBeenCalled();
  });

  it("shows only the sanitised host in the DLQ row, never the token-bearing URL (Bug-8350)", async () => {
    // The operator's own URL field legitimately shows what they typed. The
    // DLQ table must not re-surface it: it is rendered from `target_host`,
    // the deliberately path-less hint, so a secret embedded in the path or
    // query cannot reappear in a table an operator screenshots or shares.
    mocks.listWebhookDlq.mockResolvedValue([DLQ_ROW]);
    renderPanel({
      webhook_url: "https://receiver.example/hooks/bearer-tok-123?key=shh",
    });

    const row = await screen.findByRole("row", { name: /turn\.completed/ });
    expect(row.textContent).toContain("https://receiver.example");
    expect(row.textContent).not.toContain("bearer-tok-123");
    expect(row.textContent).not.toContain("key=shh");
  });
});
