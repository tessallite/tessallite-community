/**
 * Bug-7392: normalizeAgentSemanticQuery unit tests.
 *
 * The agent-service produces semantic_query with {where, having, sort}
 * while the plugin's SemanticQuery uses {filters, order}. Without
 * normalization, the re-fetch for truncated agent-chat inserts silently
 * drops all filters and ordering, inserting UNFILTERED numbers (WRONG
 * NUMBERS).
 *
 * These tests verify that every agent-schema field is correctly mapped
 * to the plugin schema.
 */
import { describe, it, expect } from 'vitest';
import { normalizeAgentSemanticQuery } from '../utils/semanticQueryNormalizer';

describe('normalizeAgentSemanticQuery', () => {
  // ---- Basic passthrough ----

  it('preserves measures and dimensions from agent schema', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['revenue', 'cost'],
      dimensions: ['region', 'year'],
    });
    expect(result.measures).toEqual(['revenue', 'cost']);
    expect(result.dimensions).toEqual(['region', 'year']);
  });

  it('returns empty arrays for missing measures/dimensions', () => {
    const result = normalizeAgentSemanticQuery({});
    expect(result.measures).toEqual([]);
    expect(result.dimensions).toEqual([]);
  });

  it('handles null/undefined input gracefully', () => {
    expect(normalizeAgentSemanticQuery(null)).toEqual({ measures: [], dimensions: [] });
    expect(normalizeAgentSemanticQuery(undefined)).toEqual({ measures: [], dimensions: [] });
  });

  // ---- WHERE -> FILTERS (Bug-7392 core fix) ----

  it('converts agent where entries ({name, op, value}) to plugin filters ({dimension, operator, values})', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: ['region'],
      where: [
        { name: 'region', op: 'eq', value: 'US' },
        { name: 'year', op: 'gte', value: 2024 },
      ],
      having: [],
      sort: [],
    });
    expect(result.filters).toEqual([
      { dimension: 'region', operator: 'eq', values: ['US'] },
      { dimension: 'year', operator: 'gte', values: ['2024'] },
    ]);
  });

  it('converts agent where entries with array values (in operator)', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: [],
      where: [
        { name: 'region', op: 'in', value: ['US', 'UK', 'DE'] },
      ],
    });
    expect(result.filters).toEqual([
      { dimension: 'region', operator: 'in', values: ['US', 'UK', 'DE'] },
    ]);
  });

  it('handles is_null operator with no value', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: [],
      where: [
        { name: 'notes', op: 'is_null', value: null },
      ],
    });
    expect(result.filters).toEqual([
      { dimension: 'notes', operator: 'is_null', values: undefined },
    ]);
  });

  it('merges where + having into a single filters array', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['revenue'],
      dimensions: ['region'],
      where: [
        { name: 'region', op: 'eq', value: 'US' },
      ],
      having: [
        { name: 'revenue', op: 'gt', value: 1000 },
      ],
    });
    expect(result.filters).toHaveLength(2);
    expect(result.filters![0]).toEqual({ dimension: 'region', operator: 'eq', values: ['US'] });
    expect(result.filters![1]).toEqual({ dimension: 'revenue', operator: 'gt', values: ['1000'] });
  });

  // ---- SORT -> ORDER ----

  it('converts agent sort entries ({name, direction}) to plugin order record', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: ['region'],
      sort: [
        { name: 'amount', direction: 'desc' },
        { name: 'region', direction: 'asc' },
      ],
    });
    expect(result.order).toEqual({
      amount: 'desc',
      region: 'asc',
    });
  });

  it('defaults sort direction to desc when missing', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: [],
      sort: [{ name: 'amount' }],
    });
    expect(result.order).toEqual({ amount: 'desc' });
  });

  // ---- LIMIT ----

  it('preserves limit', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: [],
      limit: 50,
    });
    expect(result.limit).toBe(50);
  });

  // ---- PLUGIN SCHEMA PASSTHROUGH ----

  it('passes through existing plugin-schema filters when present', () => {
    const existingFilters = [
      { dimension: 'region', operator: 'eq', values: ['US'] },
    ];
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: [],
      filters: existingFilters,
    });
    expect(result.filters).toEqual(existingFilters);
  });

  it('passes through existing plugin-schema order when present', () => {
    const existingOrder = { amount: 'desc' as const };
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: [],
      order: existingOrder,
    });
    expect(result.order).toEqual(existingOrder);
  });

  // ---- FULL AGENT TURN SHAPE (integration-style) ----

  it('correctly normalizes a full agent-service semantic_query shape', () => {
    // This is the exact shape the agent-service produces (pipeline.py ~2055)
    const agentSQ = {
      model_id: 'model-uuid',
      measures: ['net_amount', 'unit_count'],
      dimensions: ['product_category', 'quarter'],
      where: [
        { name: 'region', op: 'eq', value: 'EMEA' },
        { name: 'year', op: 'gte', value: 2024 },
      ],
      having: [
        { name: 'net_amount', op: 'gt', value: 10000 },
      ],
      sort: [
        { name: 'net_amount', direction: 'desc' },
      ],
      limit: 25,
      executed_sql: 'SELECT ...',
      shape: { rows: 25, cols: 4 },
    };
    const result = normalizeAgentSemanticQuery(agentSQ);

    expect(result.measures).toEqual(['net_amount', 'unit_count']);
    expect(result.dimensions).toEqual(['product_category', 'quarter']);
    expect(result.filters).toEqual([
      { dimension: 'region', operator: 'eq', values: ['EMEA'] },
      { dimension: 'year', operator: 'gte', values: ['2024'] },
      { dimension: 'net_amount', operator: 'gt', values: ['10000'] },
    ]);
    expect(result.order).toEqual({ net_amount: 'desc' });
    expect(result.limit).toBe(25);
  });

  // ---- EDGE: no where/having/sort ----

  it('returns undefined filters/order when agent query has no where/having/sort', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: ['region'],
    });
    expect(result.filters).toBeUndefined();
    expect(result.order).toBeUndefined();
  });

  // ---- EDGE: empty where/having/sort arrays ----

  it('returns undefined filters/order for empty where/having/sort arrays', () => {
    const result = normalizeAgentSemanticQuery({
      measures: ['amount'],
      dimensions: [],
      where: [],
      having: [],
      sort: [],
    });
    expect(result.filters).toBeUndefined();
    expect(result.order).toBeUndefined();
  });
});
