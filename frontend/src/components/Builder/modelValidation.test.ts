import { describe, expect, it } from "vitest";
import type { Join, ModelAlert, ModelTable } from "../../api/types";
import { computeStructuralIssues, mapAlertsToIssues } from "./modelValidation";

// Plain-English translator stub mirroring useT's en.json lookups.
const t = (key: string, vars?: Record<string, string>) => {
  const messages: Record<string, string> = {
    "validation.noFactTable":
      "No fact table — classify at least one table as a fact so measures can be queried",
    "validation.isolatedTable":
      'Table "{{name}}" is not joined to any other table',
    "validation.noTarget":
      "No query target set — aggregates and pocket tables cannot be materialised",
    "validation.category.invalid_dimension": "Invalid dimension",
    "validation.category.invalid_measure": "Invalid measure",
    "validation.category.refresh_failure": "Refresh failure",
    "validation.category.optimiser_failure": "Optimiser failure",
    "validation.category.data_quality": "Data quality",
  };
  let text = messages[key] ?? key;
  for (const [k, v] of Object.entries(vars ?? {})) {
    text = text.replace(`{{${k}}}`, v);
  }
  return text;
};

function table(partial: Partial<ModelTable> & { id: string }): ModelTable {
  return {
    id: partial.id,
    model_id: "m1",
    source_id: partial.source_id ?? "src-1",
    table_type: partial.table_type ?? "dimension",
    physical_name: partial.physical_name ?? partial.id,
    alias: partial.alias ?? partial.id,
    display_name: partial.display_name ?? partial.id,
    row_count_estimate: null,
    last_stats_at: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    ...partial,
  };
}

function join(left: string, right: string): Join {
  return {
    id: `${left}-${right}`,
    left_table_id: left,
    right_table_id: right,
    join_type: "inner",
    left_column_id: "c1",
    right_column_id: "c2",
    left_column_name: "id",
    right_column_name: "id",
  };
}

/** Mock generated from the backend ModelAlertResponse schema
 *  (shared/schemas/domains/dimensions_measures.py) — every field present. */
function alert(partial: Partial<ModelAlert>): ModelAlert {
  return {
    id: "a1",
    model_id: "m1",
    severity: "warning",
    category: "invalid_dimension",
    title: "Dimension is structurally invalid",
    detail: null,
    related_object_type: null,
    related_object_id: null,
    first_seen_at: "2026-06-12T00:00:00Z",
    last_seen_at: "2026-06-12T00:00:00Z",
    occurrence_count: 1,
    resolved_at: null,
    dismissed_at: null,
    dismissed_by: null,
    ...partial,
  };
}

describe("computeStructuralIssues", () => {
  it("reports nothing for an empty model (still being assembled)", () => {
    expect(computeStructuralIssues(t, [], [], false)).toEqual([]);
  });

  it("reports nothing for a healthy star model", () => {
    const tables = [
      table({ id: "fact", table_type: "fact" }),
      table({ id: "dim" }),
    ];
    const issues = computeStructuralIssues(t, tables, [join("fact", "dim")], true);
    expect(issues).toEqual([]);
  });

  it("flags a model with no fact table", () => {
    const tables = [table({ id: "dim-a" }), table({ id: "dim-b" })];
    const issues = computeStructuralIssues(
      t,
      tables,
      [join("dim-a", "dim-b")],
      true,
    );
    expect(issues).toHaveLength(1);
    expect(issues[0].severity).toBe("warning");
    expect(issues[0].message).toMatch(/no fact table/i);
  });

  it("flags isolated tables with click-to-navigate metadata", () => {
    const tables = [
      table({ id: "fact", table_type: "fact" }),
      table({ id: "dim-joined" }),
      table({ id: "dim-orphan", display_name: "Orphan Dim", source_id: "src-9" }),
    ];
    const issues = computeStructuralIssues(
      t,
      tables,
      [join("fact", "dim-joined")],
      true,
    );
    expect(issues).toHaveLength(1);
    expect(issues[0].message).toContain('Table "Orphan Dim" is not joined');
    expect(issues[0].severity).toBe("warning");
    expect(issues[0].tableId).toBe("dim-orphan");
    expect(issues[0].sourceId).toBe("src-9");
  });

  it("does not flag a single-table model as isolated", () => {
    const issues = computeStructuralIssues(
      t,
      [table({ id: "fact", table_type: "fact" })],
      [],
      true,
    );
    expect(issues).toEqual([]);
  });

  it("Bug-8614 accepts a single table without an explicit fact designation", () => {
    const issues = computeStructuralIssues(
      t,
      [table({ id: "only-dimension", table_type: "dimension" })],
      [],
      true,
    );
    expect(issues).toEqual([]);
  });

  it("notes a missing query target as info", () => {
    const tables = [
      table({ id: "fact", table_type: "fact" }),
      table({ id: "dim" }),
    ];
    const issues = computeStructuralIssues(t, tables, [join("fact", "dim")], false);
    expect(issues).toHaveLength(1);
    expect(issues[0].severity).toBe("info");
    expect(issues[0].message).toMatch(/target/i);
  });
});

describe("mapAlertsToIssues", () => {
  it("maps a structural-validator alert to a navigable tray issue", () => {
    const issues = mapAlertsToIssues(t, [
      alert({
        id: "alert-uuid-1",
        severity: "warning",
        category: "invalid_dimension",
        title: "Dimension is structurally invalid",
        detail: 'Dimension "Region" references missing column region_code',
        related_object_type: "dimension",
        related_object_id: "dim-uuid-7",
      }),
    ]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toMatchObject({
      id: "alert-alert-uuid-1",
      severity: "warning",
      message:
        'Invalid dimension: Dimension "Region" references missing column region_code',
      affectedType: "dimension",
      affectedObject: "dim-uuid-7",
    });
  });

  it("maps critical severity to error and falls back to the title", () => {
    const issues = mapAlertsToIssues(t, [
      alert({
        severity: "critical",
        category: "invalid_measure",
        title: "Measure is structurally invalid",
        detail: null,
        related_object_type: "measure",
        related_object_id: "meas-1",
      }),
    ]);
    expect(issues[0].severity).toBe("error");
    expect(issues[0].message).toBe(
      "Invalid measure: Measure is structurally invalid",
    );
  });

  it("skips resolved and dismissed alerts", () => {
    const issues = mapAlertsToIssues(t, [
      alert({ id: "r", resolved_at: "2026-06-12T01:00:00Z" }),
      alert({ id: "d", dismissed_at: "2026-06-12T01:00:00Z" }),
    ]);
    expect(issues).toEqual([]);
  });

  it("localises the data_quality category (LOW-3) instead of falling back to English", () => {
    const issues = mapAlertsToIssues(t, [
      alert({
        category: "data_quality",
        title: "Null rate above threshold",
        detail: "column region_code is 30% null",
      }),
    ]);
    // The label comes from the i18n map, not humaniseCategory.
    expect(issues[0].message).toBe(
      "Data quality: column region_code is 30% null",
    );
  });

  it("elides a leaked model UUID from optimiser alert detail (LOW-5)", () => {
    const issues = mapAlertsToIssues(t, [
      alert({
        category: "optimiser_failure",
        title: "Optimiser failure",
        detail:
          "No LLM config found for the aggregate creator on model ed2d141a-1b2c-4d3e-8f90-0a1b2c3d4e5f",
      }),
    ]);
    // The trailing " on model <uuid>" qualifier is dropped; no raw UUID leaks.
    expect(issues[0].message).toBe(
      "Optimiser failure: No LLM config found for the aggregate creator",
    );
    expect(issues[0].message).not.toMatch(
      /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
    );
  });

  it("shortens any other bare UUID in detail to a readable prefix (LOW-5)", () => {
    const issues = mapAlertsToIssues(t, [
      alert({
        category: "refresh_failure",
        title: "Refresh failure",
        detail: "Aggregate ed2d141a-1b2c-4d3e-8f90-0a1b2c3d4e5f failed to refresh",
      }),
    ]);
    expect(issues[0].message).toBe(
      "Refresh failure: Aggregate ed2d141a… failed to refresh",
    );
  });

  it("humanises unknown categories and leaves model-level alerts non-navigable", () => {
    const issues = mapAlertsToIssues(t, [
      alert({
        category: "some_future_category",
        title: "Something new",
        related_object_type: "model",
        related_object_id: "m1",
      }),
    ]);
    expect(issues[0].message).toBe("some future category: Something new");
    expect(issues[0].affectedType).toBeUndefined();
    expect(issues[0].affectedObject).toBeUndefined();
  });
});
