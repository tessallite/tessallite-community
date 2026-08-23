import { describe, it, expect } from "vitest";
import type { Dimension, ExecuteResponse, Measure } from "../../../../api/types";
import { computePivot } from "../pivot";
import { computeTotals } from "../totals";
import { pivotToCsv } from "./csv";

function dim(name: string): Dimension {
  return {
    id: name,
    name,
    display_name: name,
    source_column_id: null,
    source_column_name: name,
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    hierarchy: null,
    is_time_dim: false,
    time_grain: null,
    redundant_partner: null,
  };
}

function measure(name: string, overrides: Partial<Measure> = {}): Measure {
  return {
    id: name,
    name,
    display_name: name,
    source_column_id: null,
    source_column_name: name,
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    measure_type: "standard",
    expression: null,
    default_agg: "sum",
    data_type: "numeric",
    format: null,
    is_additive: true,
    redundant_partner: null,
    ...overrides,
  };
}

function exec(rows: Record<string, unknown>[]): ExecuteResponse {
  return {
    rows,
    columns: Object.keys(rows[0] ?? {}),
    route_type: "source",
    aggregate_id: null,
    execution_ms: 0,
    bytes_processed: 0,
    rows_returned: rows.length,
    trace: { stages: [] },
  } as unknown as ExecuteResponse;
}

describe("pivotToCsv (F-019-08)", () => {
  const rev = measure("Revenue");
  const cost = measure("Cost");

  it("emits every measure column, not just the first", () => {
    const pivot = computePivot(
      exec([
        { region: "EMEA", Revenue: 100, Cost: 40 },
        { region: "APAC", Revenue: 80, Cost: 30 },
      ]),
      rev,
      [dim("region")],
      [],
      [cost],
    );
    const csv = pivotToCsv(pivot, rev, { extraMeasures: [cost] });
    const lines = csv.trim().split("\r\n");
    // Header carries both measure names.
    expect(lines[0]).toContain("Revenue");
    expect(lines[0]).toContain("Cost");
    // Each data row carries both values.
    const emea = lines.find((l) => l.startsWith("EMEA"))!;
    expect(emea).toContain("100");
    expect(emea).toContain("40");
  });

  it("honours the supplied row order (sort)", () => {
    const pivot = computePivot(
      exec([
        { region: "APAC", Revenue: 80 },
        { region: "EMEA", Revenue: 100 },
      ]),
      rev,
      [dim("region")],
      [],
    );
    // Default order is APAC, EMEA; force EMEA first via rowKeyOrder.
    const csv = pivotToCsv(pivot, rev, {
      rowKeyOrder: [["EMEA"], ["APAC"]],
    });
    const lines = csv.trim().split("\r\n");
    expect(lines[1].startsWith("EMEA")).toBe(true);
    expect(lines[2].startsWith("APAC")).toBe(true);
  });

  it("includes the grand-total row when grand totals are on", () => {
    const pivot = computePivot(
      exec([
        { region: "EMEA", Revenue: 100 },
        { region: "APAC", Revenue: 80 },
      ]),
      rev,
      [dim("region")],
      [],
    );
    const totals = computeTotals(pivot, rev);
    const csv = pivotToCsv(pivot, rev, {
      allTotals: new Map([[rev.name, totals]]),
      showGrandTotals: true,
    });
    const grand = csv.trim().split("\r\n").find((l) => l.startsWith("Grand Total"))!;
    expect(grand).toBeDefined();
    expect(grand).toContain("180");
  });

  // Bug-6272: when the user sorts the grid (rowKeyOrder differs from the
  // pivot's natural order), grand totals and column subtotals must align to
  // the correct row. Before the fix, the export indexed totals by sequential
  // output position instead of the original pivot index.
  it("aligns grand totals to the correct row after a header-click sort", () => {
    const pivot = computePivot(
      exec([
        { region: "APAC", channel: "Online", Revenue: 80 },
        { region: "EMEA", channel: "Retail", Revenue: 100 },
      ]),
      rev,
      [dim("region")],
      [dim("channel")],
    );
    const totals = computeTotals(pivot, rev);
    // Default pivot order is APAC(80), EMEA(100); grandCol[0]=80, grandCol[1]=100.
    // Sort to EMEA first; verify EMEA's grand total is 100, not 80.
    const csv = pivotToCsv(pivot, rev, {
      rowKeyOrder: [["EMEA"], ["APAC"]],
      allTotals: new Map([[rev.name, totals]]),
      showGrandTotals: true,
    });
    const lines = csv.trim().split("\r\n");
    const emeaLine = lines.find((l) => l.startsWith("EMEA"))!;
    const apacLine = lines.find((l) => l.startsWith("APAC"))!;
    // The grand-total column is the last value on each data row.
    const emeaValues = emeaLine.split(",");
    const apacValues = apacLine.split(",");
    const emeaGrand = emeaValues[emeaValues.length - 1];
    const apacGrand = apacValues[apacValues.length - 1];
    expect(emeaGrand).toBe("100");
    expect(apacGrand).toBe("80");
  });

  it("aligns column subtotals and grand totals after a header-click sort", () => {
    const pivot = computePivot(
      exec([
        { region: "APAC", channel: "Online", quarter: "Q1", Revenue: 30 },
        { region: "APAC", channel: "Online", quarter: "Q2", Revenue: 50 },
        { region: "EMEA", channel: "Online", quarter: "Q1", Revenue: 40 },
        { region: "EMEA", channel: "Online", quarter: "Q2", Revenue: 60 },
      ]),
      rev,
      [dim("region")],
      [dim("channel"), dim("quarter")],
    );
    const totals = computeTotals(pivot, rev);
    const csv = pivotToCsv(pivot, rev, {
      rowKeyOrder: [["EMEA"], ["APAC"]],
      allTotals: new Map([[rev.name, totals]]),
      showSubtotals: true,
      showGrandTotals: true,
    });

    const lines = csv.trim().split("\r\n");
    const emeaValues = lines.find((l) => l.startsWith("EMEA"))!.split(",");
    const apacValues = lines.find((l) => l.startsWith("APAC"))!.split(",");
    expect(emeaValues.slice(-2)).toEqual(["100", "100"]);
    expect(apacValues.slice(-2)).toEqual(["80", "80"]);
  });

  // Bug-5936: row-subtotal head labels were still hardcoded to the English
  // "Total" suffix (e.g. "EMEA Total") even when the caller supplied
  // localized `labels`, unlike the column-subtotal and grand-total labels
  // in the same export, which already honoured `labels.subtotalSuffix`.
  it("uses the supplied subtotalSuffix label on row-subtotal rows, not the hardcoded English word", () => {
    const pivot = computePivot(
      exec([
        { region: "EMEA", segment: "Enterprise", Revenue: 100 },
        { region: "EMEA", segment: "SMB", Revenue: 50 },
        { region: "APAC", segment: "Enterprise", Revenue: 80 },
      ]),
      rev,
      [dim("region"), dim("segment")],
      [],
    );
    const totals = computeTotals(pivot, rev);
    const csv = pivotToCsv(pivot, rev, {
      allTotals: new Map([[rev.name, totals]]),
      showSubtotals: true,
      labels: { subtotalSuffix: "Total-fr", grandTotal: "Grand-fr" },
    });
    const lines = csv.trim().split("\r\n");
    const emeaSubtotal = lines.find((l) => l.startsWith("EMEA Total-fr"));
    expect(emeaSubtotal).toBeDefined();
    expect(emeaSubtotal).toContain("150");
    // The unlocalized English row-subtotal label must not appear at all.
    expect(lines.some((l) => l.startsWith("EMEA Total,"))).toBe(false);
  });

  // Bug-7286: a source dimension member (row OR column) that begins with a
  // spreadsheet formula trigger must be neutralised with a leading apostrophe so
  // it cannot execute when the CSV is opened in Excel / Sheets. Genuine numbers
  // stay numeric.
  it("neutralises formula-leading dimension members in row and column headers", () => {
    const pivot = computePivot(
      exec([
        { region: "=cmd|'/C calc'!A0", channel: "+evil", Revenue: 100 },
        { region: "@SUM(1,1)", channel: "-danger", Revenue: 80 },
      ]),
      rev,
      [dim("region")],
      [dim("channel")],
    );
    const csv = pivotToCsv(pivot, rev);
    // Every dangerous value is written as literal text (leading apostrophe),
    // never as a bare formula-leading token.
    expect(csv).toContain("'=cmd|'/C calc'!A0");
    expect(csv).toContain("'@SUM(1,1)");
    expect(csv).toContain("'+evil");
    expect(csv).toContain("'-danger");
    // The raw, unguarded formula must not appear at the start of any cell.
    const lines = csv.trim().split("\r\n");
    for (const line of lines) {
      for (const cell of line.split(",")) {
        const unquoted = cell.replace(/^"|"$/g, "");
        expect(["=", "+", "@"].includes(unquoted[0] ?? "")).toBe(false);
      }
    }
    // Genuine numeric measure values are preserved without a guard prefix.
    expect(csv).toContain("100");
    expect(csv).not.toContain("'100");
  });

  // F-015-04: CSV must carry the RAW numeric value, never the display string.
  // The display formatter changes scale (percent x100), precision (rounding),
  // and type (grouping separators -> text). A downstream reconciliation must
  // read the same number the query produced.
  it("exports raw numeric values, not formatted display strings", () => {
    const ratio = measure("Margin", { format: "percent" });
    const money = measure("Sales", { format: "currency" });
    const prec = measure("Rate", { format: "decimal_2dp" });
    const pivot = computePivot(
      exec([
        { region: "EMEA", Margin: 0.125, Sales: 1234.567, Rate: 3.14159 },
      ]),
      ratio,
      [dim("region")],
      [],
      [money, prec],
    );
    const csv = pivotToCsv(pivot, ratio, { extraMeasures: [money, prec] });
    const emea = csv.trim().split("\r\n").find((l) => l.startsWith("EMEA"))!;
    const cells = emea.split(",");
    // Raw ratio 0.125 must NOT become "13%" (percent formatter x100 + round).
    expect(cells).toContain("0.125");
    expect(csv).not.toContain("13%");
    expect(csv).not.toContain("%");
    // Raw 1234.567 must NOT become a currency/grouped string.
    expect(cells).toContain("1234.567");
    expect(csv).not.toContain("1,234");
    expect(csv).not.toContain("$");
    // Raw precision preserved, not rounded to 2dp ("3.14").
    expect(cells).toContain("3.14159");
  });

  // F-015-04: very small/large raw values serialise in exponent form and must
  // stay unquoted numbers (not text), while formula-injection stays guarded.
  it("keeps scientific-notation raw values as unquoted numbers", () => {
    const tiny = measure("Rate", { format: "decimal_6" });
    const pivot = computePivot(
      exec([{ region: "EMEA", Rate: 0.0000001 }]),
      tiny,
      [dim("region")],
      [],
    );
    const csv = pivotToCsv(pivot, tiny);
    const emea = csv.trim().split("\r\n").find((l) => l.startsWith("EMEA"))!;
    // 0.0000001 -> "1e-7", unquoted (a genuine number, injection-safe).
    expect(emea).toContain("1e-7");
    expect(emea).not.toContain('"1e-7"');
  });

  // F-015-04: totals must also carry raw numbers under a scaling format.
  it("exports raw numeric totals under a percent format", () => {
    const ratio = measure("Margin", { format: "percent" });
    const pivot = computePivot(
      exec([
        { region: "EMEA", Margin: 0.2 },
        { region: "APAC", Margin: 0.3 },
      ]),
      ratio,
      [dim("region")],
      [],
    );
    const totals = computeTotals(pivot, ratio);
    const csv = pivotToCsv(pivot, ratio, {
      allTotals: new Map([[ratio.name, totals]]),
      showGrandTotals: true,
    });
    const grand = csv.trim().split("\r\n").find((l) => l.startsWith("Grand Total"))!;
    // Sum 0.5, raw — never "50%".
    expect(grand).toContain("0.5");
    expect(csv).not.toContain("%");
  });
});
