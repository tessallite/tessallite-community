/**
 * The Pop out control must be offered for exactly the charts the turn actually
 * renders. Deep review found the gate had drifted onto the Excel INSERT
 * recommendation (`recommendChartType`), which answers a different question
 * ("what should we insert into the sheet?") than the chat UI's
 * "is a chart on screen?" (`buildAutoChartSpec`). The two disagree on ordinary
 * data, leaving a visible chart with no way to pop it out.
 *
 * These tests pin the gate to the same predicate `AssistantTurn.hasChart` uses.
 */
import { describe, it, expect } from "vitest";
import { buildAutoChartSpec } from "@tessallite/shared-ui";
import { recommendChartType } from "../utils/excelCharts";
import {
  resolveVisualActionData,
  turnHasPopoutChart,
} from "../utils/chartPopout";

// Few categories with a measure — the shape that renders as a pie.
const PIE_ROWS = [
  { payment_method: "Credit card", amount: 4820344 },
  { payment_method: "Debit card", amount: 3110280 },
  { payment_method: "Cash", amount: 1180455 },
];

describe("pop-out gate matches what the turn renders", () => {
  it("the insert recommendation and the rendered chart genuinely disagree", () => {
    // Guards the premise of the fix: if these ever agree, the drift is gone and
    // this whole class of bug with it.
    const headers = Object.keys(PIE_ROWS[0]);
    const rows = PIE_ROWS.map((r) => headers.map((h) => r[h as keyof typeof r]));
    const rec = recommendChartType(headers, rows as (string | number)[][]);
    const insertWouldSayChart =
      rec.confidence === "high" &&
      (rec.chartType === "line" || rec.chartType === "columnClustered");

    const spec = buildAutoChartSpec(PIE_ROWS);
    const chatRendersChart = spec !== null && spec.kind !== "metric";

    expect(chatRendersChart).toBe(true);
    expect(insertWouldSayChart).toBe(false);
  });

  it("offers pop-out for a chart the chat renders from rows", () => {
    expect(turnHasPopoutChart(null, PIE_ROWS)).toBe(true);
  });

  it("offers pop-out for a chart artifact", () => {
    expect(turnHasPopoutChart({ chart_type: "bar" }, undefined)).toBe(true);
  });

  it("does not offer pop-out for a KPI artifact, which renders cards not a chart", () => {
    expect(turnHasPopoutChart({ chart_type: "kpi" }, PIE_ROWS)).toBe(false);
  });

  it("does not offer pop-out for a single-value result, which renders as a metric", () => {
    expect(turnHasPopoutChart(null, [{ total: 42 }])).toBe(false);
  });

  it("does not offer pop-out when there is nothing to chart", () => {
    expect(turnHasPopoutChart(null, [])).toBe(false);
    expect(turnHasPopoutChart(null, undefined)).toBe(false);
  });

  it("Bug-9828: restores action rows and pop-out eligibility from a persisted visual artifact", () => {
    const renderedOutput = JSON.stringify({
      kind: "tessallite.visual.v1",
      renderer: "echarts",
      chart_type: "pie",
      columns: ["payment_method", "amount"],
      rows: PIE_ROWS,
      include_table: true,
    });

    const { artifact, rows } = resolveVisualActionData(renderedOutput);

    expect(artifact?.kind).toBe("tessallite.visual.v1");
    expect(rows).toEqual(PIE_ROWS);
    // The same resolved rows feed table, chart, and pivot actions; the parsed
    // artifact keeps the pop-out action available after a reload.
    expect(rows[0]).toEqual(PIE_ROWS[0]);
    expect(turnHasPopoutChart(artifact, rows)).toBe(true);
  });
});
