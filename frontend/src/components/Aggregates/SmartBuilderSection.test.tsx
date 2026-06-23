import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// F-011-05: the "review gate" (register AI suggestions disabled for manual
// review) must be reachable from SmartBuilder, and the panel must stop sending
// hardcoded enable_ai_aggregation / min_confidence values.

const useAISchedulerConfigMock = vi.fn();
const useLLMConfigsMock = vi.fn();
const updateMock = vi.fn();
const triggerRunMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useAISchedulerConfig: (...a: unknown[]) => useAISchedulerConfigMock(...a),
  useLLMConfigs: (...a: unknown[]) => useLLMConfigsMock(...a),
}));

vi.mock("../../api/client", () => ({
  aiSchedulerApi: { update: (...a: unknown[]) => updateMock(...a) },
  aiOptimizerApi: { triggerRun: (...a: unknown[]) => triggerRunMock(...a) },
  optimizerApiClient: { runModelSweep: vi.fn() },
}));

vi.mock("../../i18n", () => ({
  useT: () => (key: string) => key,
}));

import SmartBuilderSection from "./SmartBuilderSection";

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

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SmartBuilderSection projectId="p-1" modelId="m-1" tenantId="acme" />
    </QueryClientProvider>,
  );
}

describe("SmartBuilderSection — review gate (F-011-05)", () => {
  beforeEach(() => {
    useAISchedulerConfigMock.mockReset();
    useLLMConfigsMock.mockReset();
    updateMock.mockReset();
    useLLMConfigsMock.mockReturnValue({ data: [] });
    updateMock.mockImplementation((_p, _m, body) =>
      Promise.resolve(makeConfig(body as Record<string, unknown>)),
    );
  });

  it("renders the review-gate toggle, reflecting enable_ai_aggregation", () => {
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ enable_ai_aggregation: false }),
      isLoading: false,
    });
    renderSection();
    const toggle = screen.getByTestId("ai-require-review-toggle").querySelector("input");
    // enable_ai_aggregation=false → require-review is ON.
    expect((toggle as HTMLInputElement).checked).toBe(true);
  });

  it("sends enable_ai_aggregation=false when review is required, with no hardcoded min_confidence", async () => {
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ enable_ai_aggregation: true, min_confidence: 0.7 }),
      isLoading: false,
    });
    renderSection();
    const user = userEvent.setup();

    const toggle = screen.getByTestId("ai-require-review-toggle").querySelector("input")!;
    await user.click(toggle);
    await user.click(screen.getByTestId("ai-save-settings"));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    const body = updateMock.mock.calls[0][2] as Record<string, unknown>;
    expect(body.enable_ai_aggregation).toBe(false);
    // min_confidence is carried from the loaded config, not a hardcoded 0.5.
    expect(body.min_confidence).toBe(0.7);
  });

  it("sends enable_ai_aggregation=true when review is not required", async () => {
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ enable_ai_aggregation: true }),
      isLoading: false,
    });
    renderSection();
    const user = userEvent.setup();
    await user.click(screen.getByTestId("ai-save-settings"));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    const body = updateMock.mock.calls[0][2] as Record<string, unknown>;
    expect(body.enable_ai_aggregation).toBe(true);
  });
});

describe("SmartBuilderSection — custom cron preservation (F-011-16c)", () => {
  beforeEach(() => {
    useAISchedulerConfigMock.mockReset();
    useLLMConfigsMock.mockReset();
    updateMock.mockReset();
    triggerRunMock.mockReset();
    useLLMConfigsMock.mockReturnValue({ data: [] });
    updateMock.mockImplementation((_p, _m, body) =>
      Promise.resolve(makeConfig(body as Record<string, unknown>)),
    );
  });

  it("preserves an API-set custom cron through a UI save instead of coercing to a preset", async () => {
    // A cron that matches no preset (every 3 hours).
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ cron_expression: "0 */3 * * *" }),
      isLoading: false,
    });
    renderSection();
    const user = userEvent.setup();

    // Change an unrelated control so save proceeds without touching frequency.
    await user.click(screen.getByTestId("ai-dry-run-toggle").querySelector("input")!);
    await user.click(screen.getByTestId("ai-save-settings"));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    const body = updateMock.mock.calls[0][2] as Record<string, unknown>;
    // The custom cron round-trips unchanged (not coerced to "0 5 * * *").
    expect(body.cron_expression).toBe("0 */3 * * *");
  });
});

describe("SmartBuilderSection — save before run (F-011-16b)", () => {
  beforeEach(() => {
    useAISchedulerConfigMock.mockReset();
    useLLMConfigsMock.mockReset();
    updateMock.mockReset();
    triggerRunMock.mockReset();
    useLLMConfigsMock.mockReturnValue({ data: [] });
  });

  it("blocks Run AI with a clear message when there are unsaved changes", async () => {
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ ai_enabled: true }),
      isLoading: false,
    });
    renderSection();
    const user = userEvent.setup();

    // Make a change without saving, then click Run AI.
    await user.click(screen.getByTestId("ai-dry-run-toggle").querySelector("input")!);
    await user.click(screen.getByTestId("run-ai-optimizer"));

    // The run is not triggered; a save-first message is shown.
    expect(triggerRunMock).not.toHaveBeenCalled();
    expect(screen.getByText("smartBuilder.saveBeforeRun")).toBeInTheDocument();
  });

  it("allows Run AI when there are no unsaved changes", async () => {
    triggerRunMock.mockResolvedValue({});
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ ai_enabled: true }),
      isLoading: false,
    });
    renderSection();
    const user = userEvent.setup();

    await user.click(screen.getByTestId("run-ai-optimizer"));
    await waitFor(() => expect(triggerRunMock).toHaveBeenCalled());
  });
});
