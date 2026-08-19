import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfirmProvider } from "../Confirm";
import type { DimensionAttributeRelationship } from "../../api/types";

// ---------------------------------------------------------------------------
// API client mocks
// ---------------------------------------------------------------------------
const listMock = vi.fn();
const createMock = vi.fn();
const updateMock = vi.fn();
const deleteMock = vi.fn();
const validateMock = vi.fn();
const downstreamUsageMock = vi.fn();

vi.mock("../../api/client", () => ({
  attributeRelationshipsApi: {
    list: (...a: unknown[]) => listMock(...a),
    create: (...a: unknown[]) => createMock(...a),
    update: (...a: unknown[]) => updateMock(...a),
    delete: (...a: unknown[]) => deleteMock(...a),
    validate: (...a: unknown[]) => validateMock(...a),
    downstreamUsage: (...a: unknown[]) => downstreamUsageMock(...a),
  },
}));

const SAMPLE_ATTRS = [
  { id: "a1", name: "customer_key", data_type: "integer", is_user_defined: false },
  { id: "a2", name: "customer_name", data_type: "text", is_user_defined: false },
  { id: "a3", name: "iso_code", data_type: "text", is_user_defined: false },
  { id: "a4", name: "fx_calc", data_type: "text", is_user_defined: true },
];

let tableAttributesState: {
  data?: typeof SAMPLE_ATTRS;
  isLoading: boolean;
  isError: boolean;
} = { data: SAMPLE_ATTRS, isLoading: false, isError: false };

const SAMPLE_JOINS = [
  {
    id: "j1",
    left_table_id: "t1",
    right_table_id: "t2",
    join_type: "left",
    left_column_id: "a1",
    right_column_id: "fa1",
    left_column_name: "customer_key",
    right_column_name: "customer_id",
  },
];

vi.mock("../../api/hooks", async () => {
  const actual = await vi.importActual<typeof import("../../api/hooks")>(
    "../../api/hooks",
  );
  return {
    ...actual,
    useTableAttributes: (_p: string, _m: string, tableId: string) => {
      // For the dim table (t1), return SAMPLE_ATTRS.
      // For the fact table (t2), return empty (no fact-side candidates in tests).
      if (tableId === "t1") return tableAttributesState;
      return { data: [], isLoading: false, isError: false };
    },
    useJoins: () => ({
      data: SAMPLE_JOINS,
      isLoading: false,
      isError: false,
    }),
  };
});

import AttributeRelationshipsSection from "./AttributeRelationshipsSection";

function makeRel(
  over: Partial<DimensionAttributeRelationship> = {},
): DimensionAttributeRelationship {
  return {
    id: "r1",
    model_id: "m1",
    dimension_id: "d1",
    key_column_id: "a1",
    key_column_name: "customer_key",
    detail_column_id: "a2",
    detail_column_name: "customer_name",
    cardinality: "BIJECTION",
    null_policy: "REJECT_NULL",
    enabled: true,
    declaration_hash: "hash1",
    verification_status: "DECLARED",
    verified_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

function renderSection(canEdit = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <AttributeRelationshipsSection
          projectId="p1"
          modelId="m1"
          dimensionId="d1"
          sourceTableId="t1"
          keyColumnName="customer_key"
          canEdit={canEdit}
        />
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

describe("AttributeRelationshipsSection", () => {
  beforeEach(() => {
    listMock.mockReset().mockResolvedValue([]);
    createMock.mockReset().mockResolvedValue(makeRel({ id: "new" }));
    updateMock.mockReset().mockResolvedValue(makeRel());
    deleteMock.mockReset().mockResolvedValue({});
    validateMock.mockReset().mockResolvedValue([]);
    downstreamUsageMock.mockReset().mockResolvedValue({
      linked_dimensions: [],
      affected_aggregates: [],
    });
  });

  it("lists existing relationships with their proof status", async () => {
    listMock.mockResolvedValue([
      makeRel({ detail_column_name: "customer_name", verification_status: "VERIFIED" }),
      makeRel({
        id: "r2",
        detail_column_name: "iso_code",
        cardinality: "FUNCTIONAL_N_TO_1",
        verification_status: "BROKEN",
      }),
    ]);
    renderSection();

    await waitFor(() => expect(screen.getByText("customer_name")).toBeTruthy());
    expect(screen.getByText("iso_code")).toBeTruthy();
    expect(screen.getByText("Proven")).toBeTruthy();
    expect(screen.getByText("Broken")).toBeTruthy();
    expect(screen.queryByText("VERIFIED")).toBeNull();
  });

  it("excludes the key column and UDA columns from the detail-column picker", async () => {
    renderSection();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Add relationship" }));

    // The multi-select list should show candidate columns.
    // Key column (customer_key) and UDA column (fx_calc) must be excluded.
    // customer_name and iso_code should be visible.
    await waitFor(() => {
      expect(screen.getByText(/customer_name/)).toBeTruthy();
      expect(screen.getByText(/iso_code/)).toBeTruthy();
    });
    // customer_key is shown as the key column label, not as a candidate.
    // fx_calc (UDA) should not appear in the candidate list.
    const checkboxes = screen.getAllByRole("checkbox");
    const labels = checkboxes.map(
      (cb) => cb.closest("li")?.textContent ?? "",
    );
    expect(labels.some((l) => l.includes("fx_calc"))).toBe(false);
  });

  it("multi-select builds N creates for selected columns", async () => {
    renderSection();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Add relationship" }));

    // Select both candidate columns.
    const checkboxes = screen.getAllByRole("checkbox");
    // Find checkboxes for customer_name and iso_code.
    for (const cb of checkboxes) {
      const li = cb.closest("li");
      if (li?.textContent?.includes("customer_name") || li?.textContent?.includes("iso_code")) {
        await user.click(cb);
      }
    }

    // Click "Add 2 selected".
    const addBtn = screen.getByRole("button", { name: /Add 2 selected/i });
    await user.click(addBtn);

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(2));
    const calls = createMock.mock.calls;
    const detailNames = calls.map((c: unknown[]) => (c[3] as { detail_column_name: string }).detail_column_name);
    expect(detailNames).toContain("customer_name");
    expect(detailNames).toContain("iso_code");
    // Cardinality is always BIJECTION.
    expect((calls[0][3] as { cardinality: string }).cardinality).toBe("BIJECTION");
  });

  it("validate renders per-column advisory badges", async () => {
    validateMock.mockResolvedValue([
      { column: "customer_name", table_id: "t1", is_bijection: true, reason: "ok" },
      { column: "iso_code", table_id: "t1", is_bijection: false, reason: "forward_violation" },
    ]);
    renderSection();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Add relationship" }));

    // Select columns.
    const checkboxes = screen.getAllByRole("checkbox");
    for (const cb of checkboxes) {
      const li = cb.closest("li");
      if (li?.textContent?.includes("customer_name") || li?.textContent?.includes("iso_code")) {
        await user.click(cb);
      }
    }

    // Click validate.
    await user.click(screen.getByRole("button", { name: /Validate/i }));

    await waitFor(() => {
      // These match the i18n en.json values.
      expect(screen.getByText(/Looks 1:1/)).toBeTruthy();
      expect(screen.getByText(/Not 1:1/)).toBeTruthy();
    });
  });

  it("shows a guidance message when the dimension has no physical key", () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <ConfirmProvider>
          <AttributeRelationshipsSection
            projectId="p1"
            modelId="m1"
            dimensionId="d1"
            sourceTableId={null}
            keyColumnName={null}
            canEdit
          />
        </ConfirmProvider>
      </QueryClientProvider>,
    );
    expect(
      screen.getByText(/require a dimension backed by a physical key column/i),
    ).toBeTruthy();
  });
});
