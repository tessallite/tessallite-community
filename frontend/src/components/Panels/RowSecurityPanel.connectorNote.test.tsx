import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

/**
 * Bug-8904 — the simulate response carries `connector_note`, a warning that the
 * server could NOT resolve the model's connector definitively and compiled the
 * previewed predicate with a fallback dialect. The preview may therefore quote
 * identifiers differently from the connector that actually runs the query.
 *
 * The field reached the browser and died there: nothing rendered it, so a
 * modeller read a possibly-wrong predicate as authoritative. These tests assert
 * the warning text comes from the SERVER PAYLOAD — not from an i18n constant —
 * and that a definitive resolution stays quiet.
 */
const simulateMock = vi.fn();

vi.mock("../../api/client", () => ({
  rowSecurityApi: {
    simulate: (...args: unknown[]) => simulateMock(...args),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
}));

const hooksState = vi.hoisted(() => ({
  rules: [] as Array<Record<string, unknown>>,
}));

vi.mock("../../api/hooks", () => ({
  useRowSecurityRules: () => ({ data: hooksState.rules, isLoading: false }),
  useDimensions: () => ({ data: [], isLoading: false }),
  useSources: () => ({ data: [], isLoading: false }),
  useAllModelTables: () => ({ data: [], isLoading: false }),
  useModel: () => ({ data: { slug: "modely" }, isLoading: false }),
}));

vi.mock("../../store/builderStore", () => ({
  useBuilderStore: (selector: (s: { readOnly: boolean }) => unknown) =>
    selector({ readOnly: false }),
}));

import RowSecurityPanel from "./RowSecurityPanel";

const SERVER_NOTE =
  "The protected dimensions in this model span more than one source connector " +
  "(bigquery, postgresql). The preview below was compiled for postgresql — the " +
  "model's primary source — so identifier quoting may differ from the connector " +
  "that actually runs the query.";

function baseResult(extra: Record<string, unknown> = {}) {
  return {
    user_identity: "alice@example.com",
    roles: ["manager"],
    active_rule_ids: ["r1"],
    compiled_predicate: '"region" = \'FR\'',
    ...extra,
  };
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
          <Routes>
            <Route path="/p/:projectId/m/:modelId" element={<RowSecurityPanel />} />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

async function runSimulate(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /simulate as user/i }));
  const dialog = await screen.findByRole("dialog");
  await user.type(
    within(dialog).getByLabelText(/user identity/i),
    "alice@example.com",
  );
  await user.click(within(dialog).getByRole("button", { name: /preview/i }));
  return dialog;
}

describe("RowSecurityPanel connector_note (Bug-8904)", () => {
  beforeEach(() => {
    simulateMock.mockReset();
    hooksState.rules = [];
  });

  it("renders the server's connector warning verbatim", async () => {
    simulateMock.mockResolvedValue(baseResult({ connector_note: SERVER_NOTE }));
    renderPanel();
    const user = userEvent.setup();

    await runSimulate(user);

    await waitFor(() => expect(simulateMock).toHaveBeenCalled());
    const payload = simulateMock.mock.calls[0][2] as { probe_query?: string };
    expect(payload.probe_query).toMatch(/GROUP BY/i);
    // The exact server string, so a future refactor cannot silently swap it for
    // a static i18n sentence that no longer reflects what the server decided.
    expect(await screen.findByText(SERVER_NOTE)).toBeTruthy();
  });

  it("shows no warning when the server resolved the connector definitively", async () => {
    simulateMock.mockResolvedValue(baseResult({ connector_note: null }));
    renderPanel();
    const user = userEvent.setup();

    await runSimulate(user);

    await waitFor(() => expect(simulateMock).toHaveBeenCalled());
    // The predicate still renders...
    expect(await screen.findByText('"region" = \'FR\'')).toBeTruthy();
    // ...but nothing warns about dialect quoting.
    expect(screen.queryByText(/identifier quoting may differ/i)).toBeNull();
  });

  it("states OR-of-named-grants composition and fail-closed limits (F-007-04 / F-007-10)", () => {
    renderPanel();
    expect(screen.getByText(/Named role grants OR together/i)).toBeInTheDocument();
    expect(screen.getByText(/Unsupported query shapes are refused/i)).toBeInTheDocument();
  });

  it("i18n-qualifies the attribute-source chip (F-007-12 / Bug-8191)", () => {
    hooksState.rules = [
      {
        id: "r-claim",
        name: "dept claim",
        is_enabled: true,
        rule_type: "role_predicate",
        predicate_expression: "in('department', USER)",
        applies_to_roles: ["analyst"],
        attribute_source: "saml_claim",
        attribute_claim_name: "department",
      },
    ];
    renderPanel();
    expect(screen.getByText("via saml_claim:department")).toBeInTheDocument();
    expect(screen.queryByText(/^via saml_claim$/)).toBeNull();
  });

  it("distinguishes privileged exemption from deny-all copy (F-007-13)", async () => {
    const user = userEvent.setup();
    simulateMock.mockResolvedValue(
      baseResult({ compiled_predicate: null, active_rule_ids: [] }),
    );
    renderPanel();
    await runSimulate(user);
    expect(
      await screen.findByText(/privileged role \(tenant admin/i),
    ).toBeTruthy();

    simulateMock.mockResolvedValue(
      baseResult({ compiled_predicate: "0 = 1", active_rule_ids: [] }),
    );
    await user.click(screen.getByRole("button", { name: /preview/i }));
    expect(await screen.findByText(/Deny-all:/i)).toBeTruthy();
    expect(screen.queryByText(/greyed-out/i)).toBeNull();
  });

  it("shows one compiled string, not a per-rule fire tree (G-007-03)", async () => {
    const user = userEvent.setup();
    simulateMock.mockResolvedValue(baseResult());
    renderPanel();
    await runSimulate(user);
    expect(await screen.findByText('"region" = \'FR\'')).toBeTruthy();
    expect(screen.queryByText(/greyed-out/i)).toBeNull();
    expect(screen.queryByText(/fire \/ not-fire/i)).toBeNull();
  });

  it("renders probe rows when simulate executed (F-007-03 / G-007-01)", async () => {
    simulateMock.mockResolvedValue(baseResult({
      executed: true,
      route_type: "source",
      columns: ["region", "cnt"],
      rows: [["FR", 12]],
      row_count: 1,
    }));
    renderPanel();
    const user = userEvent.setup();
    await runSimulate(user);
    expect(await screen.findByText("FR")).toBeTruthy();
    expect(screen.getByText("12")).toBeTruthy();
  });
});
