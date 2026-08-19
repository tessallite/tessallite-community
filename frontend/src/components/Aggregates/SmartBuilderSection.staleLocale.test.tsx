/**
 * Bug-7117 (second half) — `handleRunSweep` omitted `t` from its
 * `useCallback` dependency array, unlike every other message-producing
 * callback in this file (`handleSave`, `handleRunAI`). Because `useT()`
 * returns a fresh closure over the current `I18nContext` messages on every
 * render and is not itself memoized, a callback that does not list `t` as a
 * dependency keeps the closure captured on its FIRST render — so a locale
 * switch after mount left the rule-based sweep result rendering in whatever
 * language was active when the component first mounted, not the language
 * active when the user clicked Run.
 *
 * This test does not mock `../../i18n` — it exercises the real `useT()` /
 * `I18nContext` pair so the staleness (or its absence) is observed the same
 * way a real locale switch would produce it, not asserted against a
 * `useCallback` dependency array as an implementation detail.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext } from "../../i18n";
import SmartBuilderSection from "./SmartBuilderSection";

const useAISchedulerConfigMock = vi.fn();
const useLLMConfigsMock = vi.fn();
const runModelSweepMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useAISchedulerConfig: (...a: unknown[]) => useAISchedulerConfigMock(...a),
  useLLMConfigs: (...a: unknown[]) => useLLMConfigsMock(...a),
}));

vi.mock("../../api/client", () => ({
  aiSchedulerApi: { update: vi.fn() },
  aiOptimizerApi: { triggerRun: vi.fn() },
  optimizerApiClient: { runModelSweep: (...a: unknown[]) => runModelSweepMock(...a) },
}));

// A stand-in "translated" bundle: only overrides the one key under test, so
// every other label falls back to the real English bundle via useT()'s
// `messages[key] ?? enMessages[key] ?? key` chain.
const SWITCHED_LOCALE_MESSAGES: Record<string, string> = {
  "smartBuilder.sweepFoundAndCreated":
    "SWITCHED-LOCALE: {{candidates}} candidates, {{created}} created.",
};

/** Renders SmartBuilderSection under a real I18nContext, with a button that
 * swaps the context value to simulate a locale switch without remounting
 * the component (modelId/projectId/tenantId stay fixed throughout). */
function LocaleSwitchHarness() {
  const [messages, setMessages] = useState<Record<string, string>>({});
  return (
    <div>
      <button onClick={() => setMessages(SWITCHED_LOCALE_MESSAGES)}>
        switch-locale
      </button>
      <I18nContext.Provider value={messages}>
        <SmartBuilderSection projectId="p-1" modelId="m-1" tenantId="acme" />
      </I18nContext.Provider>
    </div>
  );
}

function makeConfig(overrides: Record<string, unknown> = {}) {
  return {
    id: "cfg-1",
    model_id: "m-1",
    ai_enabled: true,
    cron_expression: "0 5 * * *",
    lookback_hours: 168,
    max_creates_per_run: 3,
    min_confidence: 0.5,
    dry_run: false,
    enable_ai_aggregation: true,
    llm_config_id: null,
    glossary_llm_config_id: null,
    created_at: "2026-06-13T00:00:00Z",
    updated_at: "2026-06-13T00:00:00Z",
    ...overrides,
  };
}

function renderHarness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LocaleSwitchHarness />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useLLMConfigsMock.mockReturnValue({ data: [] });
  useAISchedulerConfigMock.mockReturnValue({
    data: makeConfig(),
    isLoading: false,
  });
  runModelSweepMock.mockResolvedValue({
    candidates_found: 2,
    aggregates_created: 1,
    errors: [],
  });
});

describe("SmartBuilderSection — handleRunSweep locale freshness (Bug-7117)", () => {
  it("renders the sweep result in the locale active at click time, not at mount time", async () => {
    renderHarness();
    const user = userEvent.setup();

    // Switch locale AFTER mount, BEFORE running the sweep — this is exactly
    // the sequence the bug required: handleRunSweep must be recreated by the
    // context change (which only happens if `t` is a dependency), or it runs
    // with the closure from mount.
    await user.click(screen.getByText("switch-locale"));
    await user.click(screen.getByTestId("run-pattern-sweep"));

    await waitFor(() =>
      expect(
        screen.getByText("SWITCHED-LOCALE: 2 candidates, 1 created."),
      ).toBeInTheDocument(),
    );
  });
});
