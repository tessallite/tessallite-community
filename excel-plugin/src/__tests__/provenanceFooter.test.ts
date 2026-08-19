/**
 * Bug-7417 — the inserted-table provenance footer must surface the filters and
 * ordering that produced the numbers, so two tables from the same measures but
 * different slices are distinguishable.
 */
import { describe, it, expect } from 'vitest';
import {
  buildFilterSummary,
  buildOrderSummary,
  buildQueryProvenanceParts,
  isProvenanceFooter,
  restampProvenanceFooter,
  formatProvenanceTimestamp,
  PROVENANCE_FOOTER_ROWS,
} from '../utils/provenanceFooter';

describe('buildFilterSummary (Bug-7417)', () => {
  it('returns null for no filters', () => {
    expect(buildFilterSummary(undefined)).toBeNull();
    expect(buildFilterSummary([])).toBeNull();
  });

  it('formats a single-value equality filter with the operator symbol', () => {
    expect(buildFilterSummary([{ dimension: 'region', operator: 'eq', values: ['EU'] }]))
      .toBe('Filters: region = EU');
  });

  it('formats a multi-value in filter', () => {
    expect(buildFilterSummary([{ dimension: 'region', operator: 'in', values: ['EU', 'US'] }]))
      .toBe('Filters: region in EU, US');
  });

  it('elides value lists longer than the cap', () => {
    const summary = buildFilterSummary([
      { dimension: 'country', operator: 'in', values: ['A', 'B', 'C', 'D', 'E'] },
    ]);
    expect(summary).toBe('Filters: country in A, B, C, +2');
  });

  it('joins multiple filters with a semicolon', () => {
    const summary = buildFilterSummary([
      { dimension: 'region', operator: 'eq', values: ['EU'] },
      { dimension: 'amount', operator: 'gt', values: ['100'] },
    ]);
    expect(summary).toBe('Filters: region = EU; amount > 100');
  });

  it('maps known operators to symbols and passes unknown operators through', () => {
    expect(buildFilterSummary([{ dimension: 'a', operator: 'gte', values: ['1'] }]))
      .toBe('Filters: a >= 1');
    expect(buildFilterSummary([{ dimension: 'a', operator: 'weird_op', values: ['1'] }]))
      .toBe('Filters: a weird_op 1');
  });

  it('handles a filter with no values (operator only)', () => {
    expect(buildFilterSummary([{ dimension: 'a', operator: 'eq' }]))
      .toBe('Filters: a =');
  });
});

describe('buildOrderSummary (Bug-7417)', () => {
  it('returns null for no ordering', () => {
    expect(buildOrderSummary(undefined)).toBeNull();
    expect(buildOrderSummary({})).toBeNull();
  });

  it('formats a single field + direction', () => {
    expect(buildOrderSummary({ revenue: 'desc' })).toBe('Sorted by: revenue desc');
  });

  it('formats multiple fields', () => {
    expect(buildOrderSummary({ region: 'asc', revenue: 'desc' }))
      .toBe('Sorted by: region asc, revenue desc');
  });
});

describe('buildQueryProvenanceParts (Bug-7417)', () => {
  it('returns an empty list for absent or malformed JSON (footer degrades gracefully)', () => {
    expect(buildQueryProvenanceParts(undefined)).toEqual([]);
    expect(buildQueryProvenanceParts('not json {')).toEqual([]);
  });

  it('produces filters then ordering from a full SemanticQuery', () => {
    const json = JSON.stringify({
      measures: ['revenue'],
      dimensions: ['region'],
      filters: [{ dimension: 'region', operator: 'in', values: ['EU', 'US'] }],
      order: { region: 'asc' },
    });
    expect(buildQueryProvenanceParts(json)).toEqual([
      'Filters: region in EU, US',
      'Sorted by: region asc',
    ]);
  });

  it('distinguishes two tables built from the same measures but different filters', () => {
    const euOnly = buildQueryProvenanceParts(
      JSON.stringify({ measures: ['revenue'], filters: [{ dimension: 'region', operator: 'eq', values: ['EU'] }] }),
    );
    const usOnly = buildQueryProvenanceParts(
      JSON.stringify({ measures: ['revenue'], filters: [{ dimension: 'region', operator: 'eq', values: ['US'] }] }),
    );
    expect(euOnly).not.toEqual(usOnly);
    expect(euOnly).toEqual(['Filters: region = EU']);
    expect(usOnly).toEqual(['Filters: region = US']);
  });

  it('omits segments that are absent (measures-only query)', () => {
    expect(buildQueryProvenanceParts(JSON.stringify({ measures: ['revenue'] }))).toEqual([]);
  });
});

/**
 * Bug-7397 R12-2 — the footer must survive a refresh that resizes the table.
 * The refresh MOVES and RESTAMPS the footer rather than regenerating it,
 * because only the model/persona IDS are persisted in the table's provenance --
 * regenerating would silently downgrade "Model: Sales" to a raw uuid.
 */
describe('Bug-7397 R12-2 — provenance footer identification and restamping', () => {
  it('recognises a footer we wrote, and nothing else', () => {
    expect(isProvenanceFooter('Source: Tessallite | 2026-01-01 00:00 UTC')).toBe(true);
    expect(isProvenanceFooter('Grand total')).toBe(false);
    expect(isProvenanceFooter('')).toBe(false);
    expect(isProvenanceFooter(undefined)).toBe(false);
    expect(isProvenanceFooter(42)).toBe(false);
  });

  it('replaces ONLY the trailing date, preserving model, persona and query segments', () => {
    const existing = 'Source: Tessallite | Model: Sales | Viewing as: Analyst | Filters: region = EU | 2026-01-01 00:00 UTC';
    expect(restampProvenanceFooter(existing, '2026-07-28T14:05:59Z')).toBe(
      'Source: Tessallite | Model: Sales | Viewing as: Analyst | Filters: region = EU | 2026-07-28 14:05 UTC',
    );
  });

  it('keeps a filter VALUE containing the segment separator intact (lastIndexOf, not split)', () => {
    // A split-based implementation would truncate everything after the value's
    // embedded separator and silently lose provenance segments.
    const existing = 'Source: Tessallite | Filters: label = a | b | 2026-01-01 00:00 UTC';
    expect(restampProvenanceFooter(existing, '2026-07-28T14:05:00Z')).toBe(
      'Source: Tessallite | Filters: label = a | b | 2026-07-28 14:05 UTC',
    );
  });

  it('appends a date to a footer that somehow has no segments yet', () => {
    expect(restampProvenanceFooter('Source: Tessallite', '2026-07-28T14:05:00Z')).toBe(
      'Source: Tessallite | 2026-07-28 14:05 UTC',
    );
  });

  it('formats the timestamp identically for the insert and refresh paths', () => {
    expect(formatProvenanceTimestamp('2026-07-28T14:05:59.123Z')).toBe('2026-07-28 14:05 UTC');
  });

  it('the footer occupies exactly one row (the constant both footprints derive from)', () => {
    expect(PROVENANCE_FOOTER_ROWS).toBe(1);
  });
});
