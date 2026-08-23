/**
 * Provenance footer summary builders (Bug-7417).
 *
 * The inserted-table attribution row used to show only model / persona / time.
 * A reader could not tell which filters or ordering produced the numbers, so
 * two tables built from the same measures but different slices looked identical.
 *
 * These pure helpers turn the executed SemanticQuery's `filters` and `order`
 * into short human-readable summaries for the footer. Pure + side-effect-free
 * so the formatting is unit-testable without Office.js.
 */
import { strings } from '../i18n/strings';

interface FooterFilter {
  dimension: string;
  operator: string;
  values?: string[];
}

/** The subset of SemanticQuery the footer reads. */
export interface ProvenanceQuery {
  filters?: FooterFilter[];
  order?: Record<string, 'asc' | 'desc'>;
}

/** Cap on how many filter values are listed before eliding with an ellipsis. */
const MAX_VALUES_LISTED = 3;

/**
 * Bug-7397 R12-2: the footer occupies exactly ONE row, immediately below the
 * table body. Every producer/consumer of the footer's geometry (the insert
 * path's lock rectangle, the refresh path's locked+rewritten region) derives
 * from this constant so the two can never drift apart.
 */
export const PROVENANCE_FOOTER_ROWS = 1;

/** Separator between the footer's segments (`Source: ... | Model: ... | <date>`). */
const FOOTER_SEGMENT_SEPARATOR = ' | ';

/**
 * Bug-7397 R12-2: the footer's visual styling, shared by the insert path (which
 * applies it) and the refresh path (which re-applies it at the footer's new row
 * and REVERTS it on the row the footer vacated). Both must use the same values
 * or a refreshed footer would drift out of step with a freshly inserted one.
 */
export const FOOTER_FONT_ITALIC = true;
export const FOOTER_FONT_SIZE = 9;
export const FOOTER_FONT_COLOR = '#757575';

/**
 * Excel's default body font. Used to revert the styling of a row that HELD the
 * footer and has become an ordinary data row after a growing refresh. Only the
 * three properties the footer writer sets are reverted, so the row keeps its
 * number format and table styling (a blanket format clear would not).
 */
export const BODY_FONT_SIZE = 11;
export const BODY_FONT_COLOR = '#000000';

/**
 * Render an ISO timestamp as the footer's trailing date segment
 * ("2026-07-28 14:05 UTC"). Single source of truth for both the insert path
 * (useExcel.insertProvenanceFooter) and the refresh path (tableRefresh), so a
 * refreshed footer is formatted identically to the one the insert wrote.
 */
export function formatProvenanceTimestamp(isoTimestamp: string): string {
  return `${isoTimestamp.slice(0, 16).replace('T', ' ')} UTC`;
}

/**
 * Is this cell value a Tessallite provenance footer we wrote?
 *
 * Bug-7397 R12-2: the refresh path only ever MOVES/RESTAMPS a footer it can
 * positively identify. If the cell below the table holds anything else (the
 * user's own note, a total, or nothing at all), the refresh leaves it alone
 * rather than manufacturing a footer over content it does not own.
 */
export function isProvenanceFooter(value: unknown): value is string {
  return typeof value === 'string' && value.startsWith(strings.provenance.source);
}

/**
 * Rebuild an existing footer with a new timestamp, preserving every descriptive
 * segment ahead of it (model, persona, filters, ordering).
 *
 * Bug-7397 R12-2: a refresh cannot regenerate the model/persona LABELS -- only
 * their ids are persisted in the table's provenance -- so regenerating the
 * footer from metadata would silently downgrade "Model: Sales" to a raw uuid.
 * Restamping keeps the human-readable text the insert produced. The date is
 * always the LAST segment, so `lastIndexOf` is used rather than a split: a
 * filter VALUE containing the separator would otherwise truncate the footer.
 */
export function restampProvenanceFooter(existing: string, isoTimestamp: string): string {
  const dateSegment = formatProvenanceTimestamp(isoTimestamp);
  const cut = existing.lastIndexOf(FOOTER_SEGMENT_SEPARATOR);
  if (cut < 0) return `${existing}${FOOTER_SEGMENT_SEPARATOR}${dateSegment}`;
  return `${existing.slice(0, cut)}${FOOTER_SEGMENT_SEPARATOR}${dateSegment}`;
}

/** Join footer segments in the canonical order/format. */
export function joinProvenanceFooter(parts: string[]): string {
  return parts.join(FOOTER_SEGMENT_SEPARATOR);
}

/**
 * Filter-operator display symbols. These are the wire-protocol operators the
 * query-router accepts, rendered as their universal comparison symbols. They
 * are not translatable prose (`=`, `>`, `in` read the same in every locale),
 * so they live here rather than in the i18n string table. An unmapped operator
 * falls back to its raw wire name.
 */
const OPERATOR_SYMBOLS: Record<string, string> = {
  eq: '=',
  ne: '!=',
  gt: '>',
  gte: '>=',
  lt: '<',
  lte: '<=',
  in: 'in',
  not_in: 'not in',
  contains: 'contains',
  between: 'between',
};

function operatorLabel(operator: string): string {
  return OPERATOR_SYMBOLS[operator] ?? operator;
}

/**
 * Build the "Filters: region = EU, US; amount > 100" summary. Returns null
 * when there are no filters, so the caller can omit the segment entirely
 * rather than print an empty "Filters:" label.
 */
export function buildFilterSummary(filters: FooterFilter[] | undefined): string | null {
  if (!filters || filters.length === 0) return null;
  const parts: string[] = [];
  for (const f of filters) {
    if (!f || !f.dimension) continue;
    const op = operatorLabel(f.operator);
    const vals = Array.isArray(f.values) ? f.values : [];
    let valueStr: string;
    if (vals.length === 0) {
      valueStr = '';
    } else if (vals.length <= MAX_VALUES_LISTED) {
      valueStr = vals.join(', ');
    } else {
      valueStr = `${vals.slice(0, MAX_VALUES_LISTED).join(', ')}, +${vals.length - MAX_VALUES_LISTED}`;
    }
    parts.push(valueStr ? `${f.dimension} ${op} ${valueStr}` : `${f.dimension} ${op}`);
  }
  if (parts.length === 0) return null;
  return `${strings.provenance.filters}: ${parts.join('; ')}`;
}

/**
 * Build the "Sorted by: revenue desc, region asc" summary. Returns null when
 * no ordering was applied.
 */
export function buildOrderSummary(order: Record<string, 'asc' | 'desc'> | undefined): string | null {
  if (!order) return null;
  const entries = Object.entries(order);
  if (entries.length === 0) return null;
  const parts = entries.map(([field, dir]) => `${field} ${dir}`);
  return `${strings.provenance.sortedBy}: ${parts.join(', ')}`;
}

/**
 * Parse the metadata `semanticQuery` JSON string and build the ordered list of
 * provenance segments (filters, then ordering). Never throws — a malformed or
 * absent string yields an empty list so the footer degrades to its
 * model/persona/time form.
 */
export function buildQueryProvenanceParts(semanticQueryJson: string | undefined): string[] {
  if (!semanticQueryJson) return [];
  let parsed: ProvenanceQuery;
  try {
    parsed = JSON.parse(semanticQueryJson) as ProvenanceQuery;
  } catch {
    return [];
  }
  const parts: string[] = [];
  const filterSummary = buildFilterSummary(parsed.filters);
  if (filterSummary) parts.push(filterSummary);
  const orderSummary = buildOrderSummary(parsed.order);
  if (orderSummary) parts.push(orderSummary);
  return parts;
}
