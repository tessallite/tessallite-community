import { getTableMetadata } from './workbookMetadata';

/** A single drill-through cell coordinate, in the backend's request shape
 * (DrillThroughFilter — drill_routes.py): {column, op, value}. */
export interface DrillFilter {
  column: string;
  op: string;
  value: unknown;
}

export interface CellContext {
  type: 'cube-formula' | 'plugin-table' | 'unknown';
  measureId?: string;
  measureName?: string;
  /** Cell coordinates — the row's dimension values — as backend grouping levels. */
  groupingLevels?: DrillFilter[];
  /** Slicer/member filters that constrain the selected value but are not row coordinates. */
  filters?: DrillFilter[];
  /** @deprecated Use groupingLevels for row coordinates and filters for slicers. */
  drillFilters?: DrillFilter[];
  projectId?: string;
  modelId?: string;
  personaId?: string;
  conversationId?: string;
  turnId?: string;
  unavailableReason?: string;
}

export interface MeasureLookup {
  byName: Map<string, string>;
  byDisplayName: Map<string, string>;
}

export function buildDrillRequestContext(
  ctx: CellContext,
  fallbackPersonaId?: string | null,
): Record<string, unknown> {
  return {
    grouping_levels: ctx.groupingLevels || [],
    filters: ctx.filters || [],
    persona_id: ctx.personaId || fallbackPersonaId || undefined,
  };
}

const CUBE_VALUE_RE = /^=CUBEVALUE\(/i;
const CUBE_MEMBER_RE = /^=CUBEMEMBER\(/i;
const MEASURE_EXPR_RE = /\[Measures\]\.\[([^\]]+)\]/i;
const UNSUPPORTED_CUBE_DRILL_CODE = 'cube_filter_context_unrecoverable';

export async function resolveCellContext(
  address: string,
  value: unknown,
  formula: string,
  measureLookup?: MeasureLookup,
): Promise<CellContext> {
  if (formula && (CUBE_VALUE_RE.test(formula) || CUBE_MEMBER_RE.test(formula))) {
    return resolveCubeFormulaContext(formula, measureLookup);
  }

  const metadata = await getTableMetadata(address);
  if (metadata && (metadata.projectId || metadata.conversationId)) {
    return resolvePluginTableContext(address, metadata);
  }

  return { type: 'unknown' };
}

function resolveCubeFormulaContext(formula: string, measureLookup?: MeasureLookup): CellContext {
  const args = parseCubeFunctionArgs(formula);
  const match = formula.match(MEASURE_EXPR_RE);
  if (match) {
    const measureName = unescapeMdxBracketContent(match[1]);
    let measureId: string | undefined;
    if (measureLookup) {
      measureId = measureLookup.byDisplayName.get(measureName) || measureLookup.byName.get(measureName);
    }
    const filterArgs = CUBE_VALUE_RE.test(formula) ? args.slice(2) : [];
    const drillFilters: DrillFilter[] = [];
    for (const arg of filterArgs) {
      if (!arg) continue;
      const parsed = parseCubeMemberFilter(arg);
      if (!parsed) {
        return {
          type: 'cube-formula',
          measureName,
          measureId,
          unavailableReason: UNSUPPORTED_CUBE_DRILL_CODE,
        };
      }
      drillFilters.push(parsed);
    }
    return {
      type: 'cube-formula',
      measureName,
      measureId,
      filters: drillFilters.length > 0 ? drillFilters : undefined,
      drillFilters: drillFilters.length > 0 ? drillFilters : undefined,
    };
  }
  return { type: 'cube-formula' };
}

function parseCubeFunctionArgs(formula: string): string[] {
  const open = formula.indexOf('(');
  const close = formula.lastIndexOf(')');
  if (open < 0 || close <= open) return [];
  const body = formula.slice(open + 1, close);
  const args: string[] = [];
  let current = '';
  let inString = false;
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (ch === '"') {
      if (inString && body[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inString = !inString;
      }
      continue;
    }
    if (ch === ',' && !inString) {
      args.push(current.trim());
      current = '';
      continue;
    }
    current += ch;
  }
  if (current || body.endsWith(',')) args.push(current.trim());
  return args;
}

function parseCubeMemberFilter(arg: string): DrillFilter | null {
  const parts = parseMdxBracketSegments(arg);
  if (parts.length < 3) return null;
  const member = parts[parts.length - 1];
  const dimension = parts[parts.length - 2];
  if (!dimension || member === undefined || parts[0].toLowerCase() === 'measures') {
    return null;
  }
  return { column: dimension, op: 'eq', value: member };
}

function parseMdxBracketSegments(expr: string): string[] {
  const parts: string[] = [];
  for (let i = 0; i < expr.length; i++) {
    if (expr[i] !== '[') continue;
    let segment = '';
    i += 1;
    for (; i < expr.length; i++) {
      const ch = expr[i];
      if (ch === ']' && expr[i + 1] === ']') {
        segment += ']';
        i += 1;
        continue;
      }
      if (ch === ']') {
        parts.push(segment);
        break;
      }
      segment += ch;
    }
  }
  return parts;
}

function unescapeMdxBracketContent(value: string): string {
  return value.replace(/\]\]/g, ']');
}

function parseCellRef(cell: string): { col: number; row: number } {
  const match = cell.match(/^([A-Z]+)(\d+)$/i);
  if (!match) return { col: 0, row: 0 };
  const colStr = match[1].toUpperCase();
  let col = 0;
  for (let i = 0; i < colStr.length; i++) {
    col = col * 26 + (colStr.charCodeAt(i) - 64);
  }
  return { col: col - 1, row: parseInt(match[2], 10) - 1 };
}

async function resolvePluginTableContext(
  cellAddress: string,
  metadata: Partial<import('./workbookMetadata').TableMetadata>,
): Promise<CellContext> {
  const ctx: CellContext = {
    type: 'plugin-table',
    projectId: metadata.projectId,
    modelId: metadata.modelId,
    personaId: metadata.personaId,
    conversationId: metadata.conversationId,
    turnId: metadata.turnId,
  };

  const tableStart = (metadata as Record<string, string>)?.['_tableStart'];
  if (!tableStart || !cellAddress) return ctx;

  // Parse the selected cell and table start to find column offset
  const addrParts = cellAddress.split('!');
  const cellRef = addrParts.length === 2 ? addrParts[1] : addrParts[0];
  const selected = parseCellRef(cellRef);
  const start = parseCellRef(tableStart);
  const colOffset = selected.col - start.col;

  if (colOffset < 0) return ctx;

  // Resolve column header and measure
  const columnHeadersRaw = (metadata as Record<string, string>)?.['columnHeaders'];
  if (columnHeadersRaw) {
    try {
      const columnHeaders: string[] = JSON.parse(columnHeadersRaw);
      const colHeader = columnHeaders[colOffset];
      if (colHeader) {
        ctx.measureName = colHeader;
      }
    } catch { /* ignore parse errors */ }
  }

  // measureColumns maps a column TITLE to the measure's UUID (F-025-06 fix —
  // it previously stored the measure name, which 422'd as a UUID path param).
  const measureColumnsRaw = (metadata as Record<string, string>)?.['measureColumns'];
  if (measureColumnsRaw && ctx.measureName) {
    try {
      const measureColumns: Record<string, string> = JSON.parse(measureColumnsRaw);
      const measureId = measureColumns[ctx.measureName];
      if (measureId) {
        ctx.measureId = measureId;
      }
    } catch { /* ignore parse errors */ }
  }

  // F-025-06: capture the selected cell's row coordinates. Read every
  // dimension column value on the selected row and turn it into an eq filter
  // so drill-through returns exactly that cell's detail rows (not a globally
  // unfiltered dump). Requires the dimensionColumns map (title -> semantic
  // name) written at insert time and the live row values from Excel.
  const dimensionColumnsRaw = (metadata as Record<string, string>)?.['dimensionColumns'];
  const columnHeadersRaw2 = (metadata as Record<string, string>)?.['columnHeaders'];
  if (dimensionColumnsRaw && columnHeadersRaw2) {
    try {
      const dimensionColumns: Record<string, string> = JSON.parse(dimensionColumnsRaw);
      const columnHeaders: string[] = JSON.parse(columnHeadersRaw2);
      const rowValues = await readRowValues(cellAddress, start, columnHeaders.length);
      if (rowValues) {
        const drillFilters: DrillFilter[] = [];
        columnHeaders.forEach((header, idx) => {
          const dimName = dimensionColumns[header];
          const value = rowValues[idx];
          if (dimName && value !== undefined && value !== null && value !== '') {
            drillFilters.push({ column: dimName, op: 'eq', value });
          }
        });
        if (drillFilters.length > 0) {
          ctx.groupingLevels = drillFilters;
          ctx.drillFilters = drillFilters;
        }
      }
    } catch { /* ignore — drill still works at the measure level */ }
  }

  return ctx;
}

/**
 * Read the dimension/measure cells of the selected cell's row from the
 * worksheet, starting at the table's first column. Returns the row's raw
 * values aligned to the column headers, or null if Excel is unavailable.
 */
async function readRowValues(
  cellAddress: string,
  tableStart: { col: number; row: number },
  columnCount: number,
): Promise<unknown[] | null> {
  if (typeof Excel === 'undefined' || columnCount <= 0) return null;
  const addrParts = cellAddress.split('!');
  const cellRef = addrParts.length === 2 ? addrParts[1] : addrParts[0];
  const selectedRowRef = cellRef.split(':')[0];
  const selected = parseCellRef(selectedRowRef);
  // Don't read the header row.
  if (selected.row <= tableStart.row) return null;
  try {
    return await Excel.run(async (context) => {
      const sheet = addrParts.length === 2
        ? context.workbook.worksheets.getItem(addrParts[0].replace(/^'|'$/g, '').replace(/''/g, "'"))
        : context.workbook.worksheets.getActiveWorksheet();
      const range = sheet.getRangeByIndexes(selected.row, tableStart.col, 1, columnCount);
      range.load('values');
      await context.sync();
      return range.values[0] ?? null;
    });
  } catch {
    return null;
  }
}
