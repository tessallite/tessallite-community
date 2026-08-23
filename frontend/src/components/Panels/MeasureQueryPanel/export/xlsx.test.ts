import ExcelJS from "exceljs";
import { describe, it, expect } from "vitest";
import type { Dimension, ExecuteResponse, Measure } from "../../../../api/types";
import { computePivot } from "../pivot";
import { computeTotals } from "../totals";
import { pivotToXlsx } from "./xlsx";

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
    source_column_name: null,
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

async function blobToArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
  if (typeof blob.arrayBuffer === "function") {
    return blob.arrayBuffer();
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

async function readRows(blob: Blob): Promise<unknown[][]> {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.load(await blobToArrayBuffer(blob));
  const worksheet = workbook.getWorksheet("Pivot");
  expect(worksheet).toBeDefined();
  const rows: unknown[][] = [];
  worksheet!.eachRow((row) => {
    rows.push(row.values as unknown[]);
  });
  return rows;
}

async function readWorkbook(blob: Blob): Promise<ExcelJS.Workbook> {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.load(await blobToArrayBuffer(blob));
  return workbook;
}

describe("pivotToXlsx percent value integrity (F-015-01)", () => {
  it("writes the RAW ratio and delegates x100 to the native Excel percent format", async () => {
    const pctMeasure = measure("Margin", { format: "percent" as never });
    const pivot = computePivot(
      exec([
        { region: "APAC", Margin: 0.25 },   // displays 25%
        { region: "EMEA", Margin: 1.5 },    // displays 150%
        { region: "NA", Margin: 0.008 },    // displays ~1%
      ]),
      pctMeasure,
      [dim("region")],
      [],
    );

    const blob = await pivotToXlsx(pivot, pctMeasure);
    const workbook = await readWorkbook(blob);
    const ws = workbook.getWorksheet("Pivot")!;
    const rows: (string | number | null)[][] = [];
    ws.eachRow((row) => rows.push(row.values as (string | number | null)[]));

    // Row 0 is the header; data starts at index 1.
    const apac = rows.find((r) => r[1] === "APAC")!;
    const emea = rows.find((r) => r[1] === "EMEA")!;
    const na = rows.find((r) => r[1] === "NA")!;

    // The stored cell VALUE is the untouched engine ratio — never pre-scaled.
    // A user who sums/charts/re-imports the column reads the real number, not a
    // 100x-inflated one. Excel's `0%` format renders it as "25%" for display.
    expect(apac[2]).toBeCloseTo(0.25, 10);
    expect(emea[2]).toBeCloseTo(1.5, 10);
    expect(na[2]).toBeCloseTo(0.008, 10);

    // The DISPLAY scaling lives in the native percent number format.
    const dataCell = ws.getCell(
      rows.findIndex((r) => r[1] === "APAC") + 1,
      2,
    );
    expect(dataCell.numFmt).toBe("0%");
  });

  it("percent_2dp keeps the raw ratio with a 0.00% display format", async () => {
    const pctMeasure = measure("Rate", { format: "percent_2dp" as never });
    const pivot = computePivot(
      exec([{ region: "X", Rate: 1.01 }]),
      pctMeasure,
      [dim("region")],
      [],
    );
    const blob = await pivotToXlsx(pivot, pctMeasure);
    const ws = (await readWorkbook(blob)).getWorksheet("Pivot")!;
    const dataRow = (ws.getRow(2).values as (string | number | null)[]);
    expect(dataRow[2]).toBeCloseTo(1.01, 10); // raw, not 101
    expect(ws.getCell(2, 2).numFmt).toBe("0.00%");
  });
});

describe("pivotToXlsx totals", () => {
  const rev = measure("Revenue");

  it("aligns column subtotals and grand totals after a header-click sort", async () => {
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
    const blob = await pivotToXlsx(pivot, rev, {
      rowKeyOrder: [["EMEA"], ["APAC"]],
      allTotals: new Map([[rev.name, totals]]),
      showSubtotals: true,
      showGrandTotals: true,
    });

    const rows = await readRows(blob);
    const emea = rows.find((row) => row[1] === "EMEA")!;
    const apac = rows.find((row) => row[1] === "APAC")!;
    expect(emea.slice(-2)).toEqual([100, 100]);
    expect(apac.slice(-2)).toEqual([80, 80]);
  });

  it("writes localized worksheet, subtotal, and grand-total labels with the exported values", async () => {
    const pivot = computePivot(
      exec([
        { region: "APAC", channel: "Online", quarter: "Q1", Revenue: 30 },
        { region: "APAC", channel: "Online", quarter: "Q2", Revenue: 50 },
        { region: "EMEA", channel: "Online", quarter: "Q1", Revenue: 40 },
        { region: "EMEA", channel: "Online", quarter: "Q2", Revenue: 60 },
      ]),
      rev,
      [dim("region"), dim("channel")],
      [dim("quarter")],
    );
    const totals = computeTotals(pivot, rev);
    const blob = await pivotToXlsx(pivot, rev, {
      rowKeyOrder: [
        ["EMEA", "Online"],
        ["APAC", "Online"],
      ],
      allTotals: new Map([[rev.name, totals]]),
      showSubtotals: true,
      showGrandTotals: true,
      labels: {
        worksheetName: "Pivot-fr",
        subtotalSuffix: "Total-fr",
        grandTotal: "Grand-fr",
      },
    });

    const workbook = await readWorkbook(blob);
    const worksheet = workbook.getWorksheet("Pivot-fr");
    expect(worksheet).toBeDefined();
    const rows: unknown[][] = [];
    worksheet!.eachRow((row) => rows.push(row.values as unknown[]));

    expect(rows[0]).toEqual([, "region", "channel", "Q1", "Q2", "Grand-fr"]);
    expect(rows.find((row) => row[1] === "EMEA")).toEqual([
      ,
      "EMEA",
      "Online",
      40,
      60,
      100,
    ]);
    expect(rows.find((row) => row[1] === "EMEA Total-fr")).toEqual([
      ,
      "EMEA Total-fr",
      "",
      40,
      60,
      100,
    ]);
    expect(rows.find((row) => row[1] === "Grand-fr")).toEqual([
      ,
      "Grand-fr",
      "",
      70,
      110,
      180,
    ]);
  });
});

// Bug-7286: ExcelJS writes a string cell beginning with "=" as a LIVE formula.
// Dimension members are source-derived, so a planted value like `=WEBSERVICE(...)`
// would execute when the workbook is opened. The export must guard every string
// cell with a leading apostrophe while keeping genuine numbers numeric.
describe("pivotToXlsx formula-injection guard (Bug-7286)", () => {
  it("neutralises formula-leading row and column dimension members", async () => {
    const rev = measure("Revenue");
    const pivot = computePivot(
      exec([
        { region: "=cmd|'/C calc'!A0", channel: "+evil", Revenue: 100 },
        { region: "@SUM(1,1)", channel: "-danger", Revenue: 80 },
      ]),
      rev,
      [dim("region")],
      [dim("channel")],
    );
    const blob = await pivotToXlsx(pivot, rev);
    const workbook = await readWorkbook(blob);
    const ws = workbook.getWorksheet("Pivot")!;

    let sawGuardedRowMember = false;
    let sawGuardedColHeader = false;
    ws.eachRow((row) => {
      (row.values as unknown[]).forEach((v) => {
        // No cell may be an ExcelJS formula object, and no string cell may begin
        // with a bare formula trigger.
        if (v && typeof v === "object" && "formula" in (v as object)) {
          throw new Error("cell was parsed as a live formula");
        }
        if (typeof v === "string") {
          expect(["=", "+", "@"].includes(v[0])).toBe(false);
          if (v === "'=cmd|'/C calc'!A0" || v === "'@SUM(1,1)") sawGuardedRowMember = true;
          if (v === "'+evil" || v === "'-danger") sawGuardedColHeader = true;
        }
      });
    });
    expect(sawGuardedRowMember).toBe(true);
    expect(sawGuardedColHeader).toBe(true);

    // Genuine numeric measure values stay numbers, not guarded text.
    const rows: unknown[][] = [];
    ws.eachRow((row) => rows.push(row.values as unknown[]));
    const dataRow = rows.find((r) =>
      typeof r[1] === "string" && (r[1] as string).startsWith("'=cmd"),
    )!;
    expect(dataRow.some((v) => v === 100)).toBe(true);
  });
});
