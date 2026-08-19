import { describe, it, expect } from "vitest";
import {
  beautifySql,
  buildAttributeRows,
  buildGlossaryIndex,
  buildModelSelectSql,
  glossaryDescription,
  resolvePersonaAttributeIds,
  rowsToCsv,
  rowsToJson,
  rowsToText,
  rowsToXlsx,
  selectableColumnNames,
  splitTopLevel,
  type AttributeRow,
  type ExportLabels,
} from "./attributeRows";
import ExcelJS from "exceljs";

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
import type { TableAttribute, ModelTable } from "../../../api/types_domains/sources_schema";
import type { Persona } from "../../../api/types_domains/drill_refresh";
import type { Dimension, Measure } from "../../../api/types_domains/dimensions";
import type { HierarchyWithLevels } from "../../../api/hooks";
import type { GlossaryEntry } from "../../../api/types_domains/aggregates_pockets";

function table(id: string, display: string): ModelTable {
  return {
    id,
    model_id: "m",
    source_id: "s",
    table_type: "table",
    physical_name: id,
    alias: id,
    display_name: display,
    row_count_estimate: null,
    last_stats_at: null,
    created_at: "",
    updated_at: "",
  };
}

function physical(id: string, name: string, over: Partial<TableAttribute> = {}): TableAttribute {
  return {
    kind: "physical",
    id,
    table_id: "t1",
    name,
    display_name: name,
    description: null,
    data_type: "varchar",
    is_user_defined: false,
    expression: null,
    validated: null,
    validation_error: null,
    ...over,
  };
}

function uda(id: string, name: string, expression: string): TableAttribute {
  return {
    kind: "user_defined",
    id,
    table_id: "t1",
    name,
    display_name: name,
    description: null,
    data_type: "numeric",
    is_user_defined: true,
    expression,
    validated: true,
    validation_error: null,
  };
}

const labels: ExportLabels = {
  headers: ["#", "Attribute", "Data type", "Display", "Description", "Type", "Source", "Formula"],
  kindPhysical: "Physical",
  kindUda: "UDA",
};

describe("buildGlossaryIndex / glossaryDescription", () => {
  it("matches glossary by term then falls back to attribute description", () => {
    const entries = [
      { term: "Revenue", definition: "Total sales" },
      { term: "Cost", definition: "" },
    ] as GlossaryEntry[];
    const idx = buildGlossaryIndex(entries);

    expect(glossaryDescription(physical("c1", "revenue"), idx)).toBe("Total sales");
    // Empty definition is ignored — falls back to attribute description.
    expect(
      glossaryDescription(physical("c2", "cost", { description: "attr desc" }), idx),
    ).toBe("attr desc");
    // No glossary, no attribute description -> empty string.
    expect(glossaryDescription(physical("c3", "qty"), idx)).toBe("");
  });

  it("first active glossary match wins on duplicate terms", () => {
    const entries = [
      { term: "Region", definition: "First" },
      { term: "region", definition: "Second" },
    ] as GlossaryEntry[];
    const idx = buildGlossaryIndex(entries);
    expect(idx.get("region")).toBe("First");
  });
});

describe("resolvePersonaAttributeIds", () => {
  const dimensions = [
    { id: "d1", source_column_id: "col_a", user_defined_attribute_id: null },
    { id: "d2", source_column_id: null, user_defined_attribute_id: "uda_x" },
    { id: "d3", source_column_id: "col_c", user_defined_attribute_id: null },
  ] as Dimension[];
  const measures = [
    { id: "me1", source_column_id: "col_m", user_defined_attribute_id: null },
  ] as Measure[];
  const hierarchies = [
    {
      id: "h1",
      levels: [
        {
          key_attribute: { id: "col_h" },
          attributes: [{ attribute: { id: "col_h2" } }],
        },
      ],
    },
  ] as unknown as HierarchyWithLevels[];

  it("returns null when no persona is selected", () => {
    expect(resolvePersonaAttributeIds(null, dimensions, measures, hierarchies)).toBeNull();
  });

  it("maps included dimensions, measures and hierarchies to attribute ids", () => {
    const persona = {
      included_dimension_ids: ["d1", "d2"],
      included_measure_ids: ["me1"],
      included_hierarchy_ids: ["h1"],
    } as Persona;
    const ids = resolvePersonaAttributeIds(persona, dimensions, measures, hierarchies);
    expect([...(ids ?? [])].sort()).toEqual(
      ["col_a", "col_h", "col_h2", "col_m", "uda_x"].sort(),
    );
    // d3 was not included, so col_c is excluded.
    expect(ids?.has("col_c")).toBe(false);
  });
});

describe("buildAttributeRows", () => {
  const tables = [table("t1", "Orders")];
  const attributesByTable = new Map<string, TableAttribute[]>([
    ["t1", [physical("col_a", "amount"), uda("uda_x", "margin", "price - cost")]],
  ]);
  const glossaryByTerm = buildGlossaryIndex([
    { term: "amount", definition: "Order amount" },
  ] as GlossaryEntry[]);

  it("builds rows for all attributes when no persona filter", () => {
    const rows = buildAttributeRows({ tables, attributesByTable, glossaryByTerm, visibleIds: null });
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      index: 1,
      name: "amount",
      kind: "physical",
      description: "Order amount",
      sourceTable: "Orders",
      formula: "",
    });
    expect(rows[1]).toMatchObject({
      index: 2,
      name: "margin",
      kind: "user_defined",
      formula: "price - cost",
    });
  });

  it("filters to the persona-visible attribute ids", () => {
    const rows = buildAttributeRows({
      tables,
      attributesByTable,
      glossaryByTerm,
      visibleIds: new Set(["uda_x"]),
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].name).toBe("margin");
    expect(rows[0].index).toBe(1);
  });
});

describe("export serializers", () => {
  const rows: AttributeRow[] = [
    {
      id: "c1",
      index: 1,
      name: "amount",
      dataType: "numeric",
      displayName: "Amount",
      description: "Order, total",
      kind: "physical",
      sourceTable: "Orders",
      formula: "",
    },
    {
      id: "u1",
      index: 2,
      name: "margin",
      dataType: "numeric",
      displayName: "Margin",
      description: "",
      kind: "user_defined",
      sourceTable: "Orders",
      formula: "price - cost",
    },
  ];

  it("csv quotes cells with commas and includes headers", () => {
    const csv = rowsToCsv(rows, labels);
    const lines = csv.split("\n");
    expect(lines[0]).toBe("#,Attribute,Data type,Display,Description,Type,Source,Formula");
    expect(lines[1]).toContain('"Order, total"');
    expect(lines[1]).toContain("Physical");
    expect(lines[2]).toContain("UDA");
    expect(lines[2]).toContain("price - cost");
  });

  it("json maps kind labels and field names", () => {
    const parsed = JSON.parse(rowsToJson(rows, labels));
    expect(parsed[0]).toMatchObject({ name: "amount", kind: "Physical", formula: "" });
    expect(parsed[1]).toMatchObject({ name: "margin", kind: "UDA", formula: "price - cost" });
  });

  it("text renders an aligned header row", () => {
    const text = rowsToText(rows, labels);
    expect(text.split("\n")[0]).toContain("Attribute");
    expect(text).toContain("margin");
  });

  // Bug-7286: source-derived attribute text (names, glossary descriptions, UDA
  // formulas) that begins with a spreadsheet formula trigger must be neutralised
  // in the CSV and XLSX exports so it cannot execute when opened in a spreadsheet.
  const injectionRows: AttributeRow[] = [
    {
      id: "x1",
      index: 1,
      name: "=cmd|'/C calc'!A0",
      dataType: "varchar",
      displayName: "@evil",
      description: "+danger",
      kind: "physical",
      sourceTable: "-payload",
      formula: "=WEBSERVICE(\"http://x\")",
    },
  ];

  it("csv neutralises formula-leading attribute values", () => {
    const csv = rowsToCsv(injectionRows, labels);
    expect(csv).toContain("'=cmd|'/C calc'!A0");
    expect(csv).toContain("'@evil");
    expect(csv).toContain("'+danger");
    expect(csv).toContain("'-payload");
    expect(csv).toContain("'=WEBSERVICE");
    // The plain integer index column stays numeric (no guard prefix).
    expect(csv.split("\n")[1].startsWith("1,")).toBe(true);
  });

  it("xlsx writes formula-leading cells as literal text, not live formulas", async () => {
    const blob = await rowsToXlsx(injectionRows, labels);
    const buffer = await blobToArrayBuffer(blob);
    const wb = new ExcelJS.Workbook();
    await wb.xlsx.load(buffer);
    const ws = wb.getWorksheet("Attributes")!;
    // Row 1 is the header; row 2 is the data row.
    const dataRow = ws.getRow(2);
    // Attribute name column (2) must be the guarded literal text, and ExcelJS
    // must NOT have parsed it into a formula object ({ formula, result }).
    const nameCell = dataRow.getCell(2);
    expect(typeof nameCell.value).toBe("string");
    expect(nameCell.value).toBe("'=cmd|'/C calc'!A0");
    const formulaCell = dataRow.getCell(8);
    expect(typeof formulaCell.value).toBe("string");
    expect(formulaCell.value).toBe("'=WEBSERVICE(\"http://x\")");
    // The index column is emitted as its numeric string ("1") and is NOT
    // guarded (no apostrophe prefix) because it is a plain number.
    expect(dataRow.getCell(1).value).toBe("1");
  });
});

describe("selectableColumnNames", () => {
  const dimensions = [
    { id: "d1", name: "region", is_hidden: false },
    { id: "d2", name: "product", is_hidden: false },
    { id: "d3", name: "secret_dim", is_hidden: true },
  ] as Dimension[];
  const measures = [
    { id: "m1", name: "revenue", measure_type: "base", is_hidden: false },
    { id: "m2", name: "margin_pct", measure_type: "calculated", is_hidden: false },
    { id: "m3", name: "revenue_ytd", measure_type: "base", variant_kind: "ytd", is_hidden: false },
    { id: "m4", name: "hidden_meas", measure_type: "base", is_hidden: true },
  ] as Measure[];

  it("includes non-hidden dimensions and base measures, excluding calculated/variant/hidden", () => {
    const names = selectableColumnNames({ dimensions, measures, persona: null });
    expect(names).toEqual(["region", "product", "revenue"]);
  });

  it("filters to the persona's included dimensions and measures", () => {
    const persona = {
      included_dimension_ids: ["d1"],
      included_measure_ids: ["m1"],
      included_hierarchy_ids: [],
    } as Persona;
    const names = selectableColumnNames({ dimensions, measures, persona });
    expect(names).toEqual(["region", "revenue"]);
  });

  it("de-duplicates repeated names", () => {
    const dims = [
      { id: "a", name: "dup", is_hidden: false },
      { id: "b", name: "dup", is_hidden: false },
    ] as Dimension[];
    expect(selectableColumnNames({ dimensions: dims, measures: [], persona: null })).toEqual([
      "dup",
    ]);
  });
});

describe("buildModelSelectSql", () => {
  it("builds a quoted SELECT over the queryable columns", () => {
    expect(buildModelSelectSql("sales", ["region", "revenue"])).toBe(
      'SELECT "region", "revenue" FROM "sales"',
    );
  });

  it("escapes embedded double quotes and returns empty when there is nothing to select", () => {
    expect(buildModelSelectSql("s", [])).toBe("");
    expect(buildModelSelectSql("", ["a"])).toBe("");
    expect(buildModelSelectSql('we"ird', ['c"ol'])).toBe('SELECT "c""ol" FROM "we""ird"');
  });
});

describe("splitTopLevel", () => {
  it("splits on top-level commas only", () => {
    expect(splitTopLevel('"a", "b", "c"')).toEqual(['"a"', '"b"', '"c"']);
  });

  it("preserves commas inside parentheses", () => {
    expect(splitTopLevel('COALESCE(a, b), CAST(x AS TEXT), "d"')).toEqual([
      "COALESCE(a, b)",
      "CAST(x AS TEXT)",
      '"d"',
    ]);
  });

  it("preserves commas inside string literals", () => {
    expect(splitTopLevel("'x, y', \"col\"")).toEqual(["'x, y'", '"col"']);
  });
});

describe("beautifySql", () => {
  it("returns empty string for blank input", () => {
    expect(beautifySql("")).toBe("");
    expect(beautifySql(null)).toBe("");
    expect(beautifySql(undefined)).toBe("");
  });

  it("puts each projected column on its own line and breaks clauses", () => {
    const out = beautifySql('SELECT "a", "b", "c" FROM "orders"');
    expect(out).toBe('SELECT\n  "a",\n  "b",\n  "c"\nFROM "orders"');
  });

  it("does not split function arguments across lines", () => {
    const out = beautifySql('SELECT COALESCE(a, b) AS x, "y" FROM t');
    expect(out).toBe('SELECT\n  COALESCE(a, b) AS x,\n  "y"\nFROM t');
  });

  it("breaks JOIN/ON and WHERE onto their own lines", () => {
    const out = beautifySql(
      'SELECT "a" FROM t1 LEFT JOIN t2 ON t1.id = t2.id WHERE t1.x = 1',
    );
    expect(out).toBe(
      'SELECT "a"\nFROM t1\nLEFT JOIN t2\n  ON t1.id = t2.id\nWHERE t1.x = 1',
    );
  });

  it("preserves a DISTINCT projection", () => {
    const out = beautifySql('SELECT DISTINCT "a", "b" FROM t');
    expect(out).toBe('SELECT DISTINCT\n  "a",\n  "b"\nFROM t');
  });
});
