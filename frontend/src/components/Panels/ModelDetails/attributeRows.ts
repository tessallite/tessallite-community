import ExcelJS from "exceljs";
import type {
  ModelTable,
  TableAttribute,
} from "../../../api/types_domains/sources_schema";
import type { GlossaryEntry } from "../../../api/types_domains/aggregates_pockets";
import type { Persona } from "../../../api/types_domains/drill_refresh";
import type { Dimension, Measure } from "../../../api/types_domains/dimensions";
import type { HierarchyWithLevels } from "../../../api/hooks";

/**
 * One row in the Model Details attribute table. `id` is the underlying
 * attribute identifier — the physical column id for physical attributes and
 * the user-defined-attribute id for UDAs — and is used for persona filtering.
 */
export interface AttributeRow {
  id: string;
  index: number;
  name: string;
  dataType: string;
  displayName: string;
  description: string;
  kind: "physical" | "user_defined";
  sourceTable: string;
  formula: string;
}

/**
 * Resolve the set of attribute ids a persona can see, by mapping its included
 * dimensions / measures / hierarchies back to the underlying physical columns
 * and UDAs they are built on. Returns `null` when no persona is selected,
 * meaning "no filter — show every attribute".
 */
export function resolvePersonaAttributeIds(
  persona: Persona | null,
  dimensions: Dimension[],
  measures: Measure[],
  hierarchies: HierarchyWithLevels[],
): Set<string> | null {
  if (!persona) return null;

  const dimIds = new Set(persona.included_dimension_ids);
  const measureIds = new Set(persona.included_measure_ids);
  const hierIds = new Set(persona.included_hierarchy_ids);
  const visible = new Set<string>();

  const addRef = (
    columnId: string | null,
    udaId: string | null,
  ): void => {
    if (udaId) visible.add(udaId);
    else if (columnId) visible.add(columnId);
  };

  for (const d of dimensions) {
    if (dimIds.has(d.id)) addRef(d.source_column_id, d.user_defined_attribute_id);
  }
  for (const m of measures) {
    if (measureIds.has(m.id)) addRef(m.source_column_id, m.user_defined_attribute_id);
  }
  for (const h of hierarchies) {
    if (!hierIds.has(h.id)) continue;
    for (const level of h.levels) {
      visible.add(level.key_attribute.id);
      for (const a of level.attributes) visible.add(a.attribute.id);
    }
  }

  return visible;
}

/**
 * Look up a business description for an attribute. Prefers a glossary entry
 * whose term matches the attribute's canonical or display name
 * (case-insensitive); falls back to the attribute's own description.
 */
export function glossaryDescription(
  attr: TableAttribute,
  glossaryByTerm: Map<string, string>,
): string {
  const byName = glossaryByTerm.get(attr.name.trim().toLowerCase());
  if (byName) return byName;
  if (attr.display_name) {
    const byDisplay = glossaryByTerm.get(attr.display_name.trim().toLowerCase());
    if (byDisplay) return byDisplay;
  }
  return attr.description ?? "";
}

export function buildGlossaryIndex(entries: GlossaryEntry[]): Map<string, string> {
  const idx = new Map<string, string>();
  for (const e of entries) {
    const key = e.term.trim().toLowerCase();
    // First active match wins; do not overwrite with later duplicates.
    if (!idx.has(key) && e.definition) idx.set(key, e.definition);
  }
  return idx;
}

/**
 * Flatten every model table's attributes into ordered rows, optionally
 * filtered to the persona-visible attribute id set.
 */
export function buildAttributeRows(args: {
  tables: ModelTable[];
  attributesByTable: Map<string, TableAttribute[]>;
  glossaryByTerm: Map<string, string>;
  visibleIds: Set<string> | null;
}): AttributeRow[] {
  const { tables, attributesByTable, glossaryByTerm, visibleIds } = args;
  const tablesById = new Map(tables.map((tb) => [tb.id, tb]));
  const rows: AttributeRow[] = [];
  let index = 0;

  for (const table of tables) {
    const attrs = attributesByTable.get(table.id) ?? [];
    for (const attr of attrs) {
      if (visibleIds && !visibleIds.has(attr.id)) continue;
      const owner = tablesById.get(attr.table_id) ?? table;
      index += 1;
      rows.push({
        id: attr.id,
        index,
        name: attr.name,
        dataType: attr.data_type,
        displayName: attr.display_name ?? attr.name,
        description: glossaryDescription(attr, glossaryByTerm),
        kind: attr.is_user_defined ? "user_defined" : "physical",
        sourceTable: owner.display_name,
        formula: attr.is_user_defined ? attr.expression ?? "" : "",
      });
    }
  }

  return rows;
}

// ---------------------------------------------------------------------------
// SELECT statement helpers
// ---------------------------------------------------------------------------

/**
 * The model's queryable column surface, as the set of names the query-router
 * binder can resolve in a SELECT: dimension names plus base-measure names.
 *
 * The binder resolves SELECT column references against dimension and measure
 * *names* only — never raw physical column names or UDA names — so building
 * the source SELECT from physical attributes would fail to bind and the router
 * would fall back to the logical query. Calculated and variant measures are
 * excluded because they have no single raw physical column (they expand to an
 * expression / window function and cannot appear in a flat detail SELECT).
 * Hidden dimensions/measures are excluded because the business view drops them.
 * When a persona is selected, only its included dimensions/measures are kept.
 */
export function selectableColumnNames(args: {
  dimensions: Dimension[];
  measures: Measure[];
  persona: Persona | null;
}): string[] {
  const { dimensions, measures, persona } = args;
  const dimIds = persona ? new Set(persona.included_dimension_ids) : null;
  const measureIds = persona ? new Set(persona.included_measure_ids) : null;
  const names: string[] = [];
  const seen = new Set<string>();
  const push = (n: string): void => {
    if (n && !seen.has(n)) {
      seen.add(n);
      names.push(n);
    }
  };

  for (const d of dimensions) {
    if (d.is_hidden) continue;
    if (dimIds && !dimIds.has(d.id)) continue;
    push(d.name);
  }
  for (const m of measures) {
    if (m.is_hidden) continue;
    if (m.measure_type === "calculated") continue;
    if (m.variant_kind) continue;
    if (measureIds && !measureIds.has(m.id)) continue;
    push(m.name);
  }
  return names;
}

/**
 * Build the logical model-level SELECT (`SELECT "a", "b" FROM "slug"`) over the
 * model's queryable columns. This is the statement sent to the query-router for
 * rewriting into the physical, joined, dialect-translated SELECT.
 */
export function buildModelSelectSql(slug: string, columnNames: string[]): string {
  if (!slug || columnNames.length === 0) return "";
  const cols = columnNames.map((n) => `"${n.replace(/"/g, '""')}"`).join(", ");
  return `SELECT ${cols} FROM "${slug.replace(/"/g, '""')}"`;
}

/**
 * Split a comma-separated list on top-level commas only — commas inside
 * parentheses (e.g. `COALESCE(a, b)`) or string literals are preserved, so a
 * projection list can be broken into one item per line without splitting
 * function arguments.
 */
export function splitTopLevel(s: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let quote: string | null = null;
  let buf = "";
  for (const ch of s) {
    if (quote) {
      buf += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      buf += ch;
      continue;
    }
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    if (ch === "," && depth === 0) {
      parts.push(buf.trim());
      buf = "";
      continue;
    }
    buf += ch;
  }
  if (buf.trim()) parts.push(buf.trim());
  return parts;
}

/**
 * Beautify a SELECT statement for read-only display: break before the major
 * clauses and put each projected column on its own indented line. Designed for
 * the Model Details source SELECT; it is presentational only, not a SQL parser.
 */
export function beautifySql(sql: string | null | undefined): string {
  if (!sql || !sql.trim()) return "";
  let s = sql.replace(/\s+/g, " ").trim();
  const keywords =
    /\b(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|LEFT JOIN|RIGHT JOIN|INNER JOIN|FULL OUTER JOIN|FULL JOIN|CROSS JOIN|JOIN|UNION ALL|UNION|ON)\b/gi;
  s = s.replace(keywords, (m) => `\n${m.toUpperCase()}`);
  s = s.replace(/[ \t]+\n/g, "\n");
  s = s.replace(/\nON\b/g, "\n  ON");

  const nl = s.indexOf("\n");
  const head = nl === -1 ? s : s.slice(0, nl);
  const tail = nl === -1 ? "" : s.slice(nl);
  const m = head.match(/^\s*SELECT\s+(DISTINCT\s+)?(.*)$/i);
  if (!m) return s.trim();

  const cols = splitTopLevel(m[2]);
  if (cols.length <= 1) return s.trim();

  const prefix = m[1] ? "SELECT DISTINCT" : "SELECT";
  const projection = cols.map((c) => `  ${c}`).join(",\n");
  return `${prefix}\n${projection}${tail}`.trim();
}

// ---------------------------------------------------------------------------
// Export serializers
// ---------------------------------------------------------------------------

export interface ExportLabels {
  headers: [string, string, string, string, string, string, string, string];
  kindPhysical: string;
  kindUda: string;
}

function rowValues(row: AttributeRow, labels: ExportLabels): string[] {
  return [
    String(row.index),
    row.name,
    row.dataType,
    row.displayName,
    row.description,
    row.kind === "user_defined" ? labels.kindUda : labels.kindPhysical,
    row.sourceTable,
    row.formula,
  ];
}

function csvCell(v: string): string {
  if (/[",\n]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
  return v;
}

export function rowsToCsv(rows: AttributeRow[], labels: ExportLabels): string {
  const lines = [labels.headers.map(csvCell).join(",")];
  for (const row of rows) {
    lines.push(rowValues(row, labels).map(csvCell).join(","));
  }
  return lines.join("\n");
}

export function rowsToJson(rows: AttributeRow[], labels: ExportLabels): string {
  const out = rows.map((row) => ({
    index: row.index,
    name: row.name,
    data_type: row.dataType,
    display_name: row.displayName,
    description: row.description,
    kind: row.kind === "user_defined" ? labels.kindUda : labels.kindPhysical,
    source_table: row.sourceTable,
    formula: row.formula,
  }));
  return JSON.stringify(out, null, 2);
}

export function rowsToText(rows: AttributeRow[], labels: ExportLabels): string {
  const matrix = [labels.headers, ...rows.map((row) => rowValues(row, labels))];
  const widths = labels.headers.map((_, c) =>
    Math.max(...matrix.map((r) => r[c].length)),
  );
  return matrix
    .map((r) => r.map((cell, c) => cell.padEnd(widths[c])).join("  ").trimEnd())
    .join("\n");
}

export async function rowsToXlsx(
  rows: AttributeRow[],
  labels: ExportLabels,
): Promise<Blob> {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet("Attributes");

  const header = ws.addRow([...labels.headers]);
  header.eachCell((cell) => {
    cell.fill = {
      type: "pattern",
      pattern: "solid",
      fgColor: { argb: "FF4472C4" },
    };
    cell.font = { bold: true, color: { argb: "FFFFFFFF" }, size: 11 };
  });

  for (const row of rows) {
    ws.addRow(rowValues(row, labels));
  }

  for (let c = 1; c <= labels.headers.length; c++) {
    const col = ws.getColumn(c);
    let max = labels.headers[c - 1].length;
    col.eachCell?.((cell) => {
      const len = String(cell.value ?? "").length;
      if (len > max) max = len;
    });
    col.width = Math.min(60, Math.max(10, max + 2));
  }

  ws.views = [{ state: "frozen", ySplit: 1 }];

  const buffer = await wb.xlsx.writeBuffer();
  return new Blob([buffer], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}
