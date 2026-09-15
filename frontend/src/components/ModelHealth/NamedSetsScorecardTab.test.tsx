import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import NamedSetsScorecardTab from "./NamedSetsScorecardTab";
import type { NamedSet } from "../../api/types";

const useNamedSetsMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useNamedSets: (...args: unknown[]) => useNamedSetsMock(...args),
}));

vi.mock("../../i18n", () => ({
  useT: () => (key: string) => key,
}));

vi.mock("../HelpIconButton", () => ({
  default: () => null,
}));

function makeSet(id: string, name: string, overrides: Partial<NamedSet> = {}): NamedSet {
  return {
    id,
    model_id: "model-1",
    name,
    display_name: name,
    description: `Description of ${name}`,
    display_folder: null,
    scope: 1,
    expression: "",
    dimensions: null,
    builder_definition: null,
    list_type: "advanced_mdx",
    certification_status: "draft",
    replacement_id: null,
    owner_user_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  } as NamedSet;
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NamedSetsScorecardTab projectId="proj-1" modelId="model-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useNamedSetsMock.mockReset();
});

describe("NamedSetsScorecardTab", () => {
  it("shows loading spinner while named sets load", () => {
    useNamedSetsMock.mockReturnValue({ data: undefined, isLoading: true });
    const { container } = renderTab();
    expect(container.querySelector("[role='progressbar']")).toBeTruthy();
  });

  it("shows empty state when no deployed named sets exist", () => {
    useNamedSetsMock.mockReturnValue({ data: [], isLoading: false });
    renderTab();
    expect(screen.getByText("namedSetsScorecard.noSetsTitle")).toBeTruthy();
  });

  // R3 (round-3 external review): a failed request must not collapse into
  // the "no deployed named sets" empty state — that misrepresents an outage
  // as a legitimate finding. Mirrors UsageAnalyticsTab's Bug-7459 guard.
  it("shows a retryable error, not the empty state, when the request fails", () => {
    const refetch = vi.fn();
    useNamedSetsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch,
    });
    renderTab();
    expect(screen.getByText("namedSetsScorecard.loadError")).toBeTruthy();
    expect(screen.queryByText("namedSetsScorecard.noSetsTitle")).toBeNull();

    screen.getByText("common.retry").click();
    expect(refetch).toHaveBeenCalled();
  });

  it("renders deployed named sets by name", () => {
    useNamedSetsMock.mockReturnValue({
      data: [makeSet("s1", "Top Customers"), makeSet("s2", "Active Regions")],
      isLoading: false,
    });
    renderTab();
    expect(screen.getByText("Top Customers")).toBeTruthy();
    expect(screen.getByText("Active Regions")).toBeTruthy();
  });

  it("groups named sets by display_folder", () => {
    useNamedSetsMock.mockReturnValue({
      data: [
        makeSet("s1", "Top Customers", { display_folder: "Sales" }),
        makeSet("s2", "Active Regions", { display_folder: "Ops" }),
      ],
      isLoading: false,
    });
    renderTab();
    expect(screen.getAllByText(/Sales/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Ops/).length).toBeGreaterThan(0);
  });

  // Bug-9091 (R2-B02): this is the first real consumer of deployedOnly for
  // named sets — the guard is that it is a VIEWER (deployed_only=true), never
  // the live authoring list. Regressing the boolean here would silently
  // reopen the exact gap the external review caught: a certified-but-
  // undeployed edit leaking into what is supposed to be the deployed view.
  it("requests the DEPLOYED named-set list, not the live authoring list", () => {
    useNamedSetsMock.mockReturnValue({ data: [], isLoading: false });
    renderTab();
    expect(useNamedSetsMock).toHaveBeenCalledWith("proj-1", "model-1", true);
  });

  it("renders title from i18n", () => {
    useNamedSetsMock.mockReturnValue({
      data: [makeSet("s1", "Top Customers")],
      isLoading: false,
    });
    renderTab();
    expect(screen.getByText("namedSetsScorecard.title")).toBeTruthy();
  });
});
