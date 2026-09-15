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
/**
 * Bug-7397 R12-2: the footer occupies exactly ONE row, immediately below the
 * table body. Every producer/consumer of the footer's geometry (the insert
 * path's lock rectangle, the refresh path's locked+rewritten region) derives
 * from this constant so the two can never drift apart.
 */
export declare const PROVENANCE_FOOTER_ROWS = 1;
/**
 * Bug-7397 R12-2: the footer's visual styling, shared by the insert path (which
 * applies it) and the refresh path (which re-applies it at the footer's new row
 * and REVERTS it on the row the footer vacated). Both must use the same values
 * or a refreshed footer would drift out of step with a freshly inserted one.
 */
export declare const FOOTER_FONT_ITALIC = true;
export declare const FOOTER_FONT_SIZE = 9;
export declare const FOOTER_FONT_COLOR = "#757575";
/**
 * Excel's default body font. Used to revert the styling of a row that HELD the
 * footer and has become an ordinary data row after a growing refresh. Only the
 * three properties the footer writer sets are reverted, so the row keeps its
 * number format and table styling (a blanket format clear would not).
 */
export declare const BODY_FONT_SIZE = 11;
export declare const BODY_FONT_COLOR = "#000000";
/**
 * Render an ISO timestamp as the footer's trailing date segment
 * ("2026-07-28 14:05 UTC"). Single source of truth for both the insert path
 * (useExcel.insertProvenanceFooter) and the refresh path (tableRefresh), so a
 * refreshed footer is formatted identically to the one the insert wrote.
 */
export declare function formatProvenanceTimestamp(isoTimestamp: string): string;
/**
 * Is this cell value a Tessallite provenance footer we wrote?
 *
 * Bug-7397 R12-2: the refresh path only ever MOVES/RESTAMPS a footer it can
 * positively identify. If the cell below the table holds anything else (the
 * user's own note, a total, or nothing at all), the refresh leaves it alone
 * rather than manufacturing a footer over content it does not own.
 */
export declare function isProvenanceFooter(value: unknown): value is string;
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
export declare function restampProvenanceFooter(existing: string, isoTimestamp: string): string;
/** Join footer segments in the canonical order/format. */
export declare function joinProvenanceFooter(parts: string[]): string;
/**
 * Build the "Filters: region = EU, US; amount > 100" summary. Returns null
 * when there are no filters, so the caller can omit the segment entirely
 * rather than print an empty "Filters:" label.
 */
export declare function buildFilterSummary(filters: FooterFilter[] | undefined): string | null;
/**
 * Build the "Sorted by: revenue desc, region asc" summary. Returns null when
 * no ordering was applied.
 */
export declare function buildOrderSummary(order: Record<string, 'asc' | 'desc'> | undefined): string | null;
/**
 * Parse the metadata `semanticQuery` JSON string and build the ordered list of
 * provenance segments (filters, then ordering). Never throws — a malformed or
 * absent string yields an empty list so the footer degrades to its
 * model/persona/time form.
 */
export declare function buildQueryProvenanceParts(semanticQueryJson: string | undefined): string[];
export {};
