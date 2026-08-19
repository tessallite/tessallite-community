/**
 * The agent eval harness had no way in.
 *
 * `agentApi.runEval` and the `POST /projects/{id}/agent/eval/run` endpoint both
 * shipped, and nothing in the SPA called either — the only way to replay a
 * project's example questions and find out whether the agent had regressed was
 * to hit the API by hand. These tests hold the three things that matter about
 * the control now that it exists: it reaches the endpoint, it states the
 * regression verdict the server computes, and it is not offered to a role the
 * server would refuse (the run spends real LLM budget).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const runEvalMock = vi.fn();
const kpisMock = vi.fn();
const logMock = vi.fn();

vi.mock("../api/agentApi", () => ({
  agentApi: {
    runEval: (...a: unknown[]) => runEvalMock(...a),
    kpis: (...a: unknown[]) => kpisMock(...a),
    log: (...a: unknown[]) => logMock(...a),
  },
}));

vi.mock("../api/hooks", () => ({
  useProject: () => ({ data: { id: "p1", display_name: "Project One" } }),
}));

vi.mock("../i18n", () => ({
  useT: () => (key: string) => key,
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useParams: () => ({ tenantId: "acme-demo", projectId: "p1" }),
}));

vi.mock("../components/HelpIconButton", () => ({ default: () => null }));

const canEditModelConfigMock = vi.fn();
vi.mock("../auth/currentUser", () => ({
  canEditModelConfig: () => canEditModelConfigMock(),
}));

import AgentLog from "./AgentLog";

const REPORT = {
  project_id: "p1",
  total: 12,
  ok: 10,
  refused: 1,
  clarify: 1,
  error: 0,
  rows: [],
  accuracy_score: 0.75,
  decomposition_regressions: 0,
  budget_stopped: null,
  decomposition_compared: 12,
  decomposition_unparseable: 0,
  regressed: false,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AgentLog />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  canEditModelConfigMock.mockReturnValue(true);
  kpisMock.mockResolvedValue({
    window_days: 7,
    total_turns: 0,
    ok_turns: 0,
    refused_turns: 0,
    citations_rate: 0,
    aggregate_route_rate: 0,
    judge_block_rate: 0,
    feedback_up: 0,
    feedback_down: 0,
    dlq_depth: 0,
  });
  logMock.mockResolvedValue({ rows: [], total: 0 });
  runEvalMock.mockResolvedValue(REPORT);
});

describe("AgentLog — the eval harness is reachable and reports its verdict", () => {
  it("runs the eval against the project in the route", async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText("agentLog.evalRunButton")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByText("agentLog.evalRunButton"));

    await waitFor(() => expect(runEvalMock).toHaveBeenCalledWith("p1"));
    await waitFor(() =>
      expect(screen.getByText("agentLog.evalNotRegressed")).toBeInTheDocument(),
    );
  });

  it("states a regression as the verdict, not just as a count", async () => {
    // The server documents `regressed` as the flag a client gating on eval
    // accuracy must treat as a FAILED run whatever ok/refused/error say. A
    // strip that only printed the counts would report this run as 10 answered
    // out of 12 and say nothing about the regression.
    runEvalMock.mockResolvedValue({
      ...REPORT,
      regressed: true,
      decomposition_regressions: 2,
    });

    renderPage();
    await waitFor(() =>
      expect(screen.getByText("agentLog.evalRunButton")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByText("agentLog.evalRunButton"));

    await waitFor(() =>
      expect(screen.getByText("agentLog.evalRegressed")).toBeInTheDocument(),
    );
    expect(screen.queryByText("agentLog.evalNotRegressed")).not.toBeInTheDocument();
  });

  it("surfaces an early stop on an exhausted budget", async () => {
    runEvalMock.mockResolvedValue({ ...REPORT, budget_stopped: "daily_token_budget" });

    renderPage();
    await waitFor(() =>
      expect(screen.getByText("agentLog.evalRunButton")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByText("agentLog.evalRunButton"));

    await waitFor(() =>
      expect(screen.getByText("agentLog.evalBudgetStopped")).toBeInTheDocument(),
    );
  });

  it("does not offer the run to a role the server would refuse", async () => {
    // The endpoint is gated on the project modeller role and every run spends
    // LLM budget; offering the button to a viewer would only produce a 403.
    canEditModelConfigMock.mockReturnValue(false);

    renderPage();
    // Wait for the page itself, so "no button" is a real absence rather than
    // an assertion made before anything rendered.
    await screen.findByLabelText("agentLog.filterToLabel");

    expect(screen.queryByText("agentLog.evalRunButton")).not.toBeInTheDocument();
    expect(runEvalMock).not.toHaveBeenCalled();
  });
});
