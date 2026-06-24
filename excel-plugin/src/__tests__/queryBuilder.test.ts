import { describe, it, expect } from 'vitest';
import type { SemanticQuery, QueryFilter } from '../types/tessallite';

interface ZoneItem {
  id: string;
  name: string;
  zone: 'filters' | 'columns' | 'values' | 'rows';
  operator?: string;
  values?: string[];
}

function buildSemanticQuery(items: ZoneItem[], limit = 1000): SemanticQuery | null {
  const values = items.filter(i => i.zone === 'values').map(i => i.id);
  const rowDims = items.filter(i => i.zone === 'rows').map(i => i.id);
  const colDims = items.filter(i => i.zone === 'columns').map(i => i.id);
  const filterItems = items.filter(i => i.zone === 'filters');

  const filters: QueryFilter[] | undefined = filterItems.length > 0
    ? filterItems.map(f => ({ member: f.id, operator: f.operator || 'set', values: f.values?.length ? f.values : undefined }))
    : undefined;

  const query: SemanticQuery = {
    measures: values.length > 0 ? values : undefined,
    dimensions: [...rowDims, ...colDims],
    filters,
    limit,
  };

  if (!query.measures?.length) return null;
  return query;
}

describe('query builder', () => {
  it('builds measures-only query', () => {
    const query = buildSemanticQuery([
      { id: 'm1', name: 'Revenue', zone: 'values' },
      { id: 'm2', name: 'Cost', zone: 'values' },
    ]);
    expect(query).not.toBeNull();
    expect(query!.measures).toEqual(['m1', 'm2']);
    expect(query!.dimensions).toEqual([]);
    expect(query!.filters).toBeUndefined();
  });

  it('builds measures + dimensions query', () => {
    const query = buildSemanticQuery([
      { id: 'm1', name: 'Revenue', zone: 'values' },
      { id: 'd1', name: 'Country', zone: 'rows' },
      { id: 'd2', name: 'Year', zone: 'columns' },
    ]);
    expect(query!.dimensions).toContain('d1');
    expect(query!.dimensions).toContain('d2');
    expect(query!.measures).toContain('m1');
  });

  it('includes filters in query', () => {
    const query = buildSemanticQuery([
      { id: 'm1', name: 'Revenue', zone: 'values' },
      { id: 'd1', name: 'Country', zone: 'filters', operator: 'equals', values: ['US', 'UK'] },
    ]);
    expect(query!.filters).toHaveLength(1);
    expect(query!.filters![0].operator).toBe('equals');
    expect(query!.filters![0].values).toEqual(['US', 'UK']);
  });

  it('uses default filter operator when not specified', () => {
    const query = buildSemanticQuery([
      { id: 'm1', name: 'Revenue', zone: 'values' },
      { id: 'd1', name: 'Country', zone: 'filters' },
    ]);
    expect(query!.filters![0].operator).toBe('set');
    expect(query!.filters![0].values).toBeUndefined();
  });

  it('applies limit parameter', () => {
    const query = buildSemanticQuery(
      [{ id: 'm1', name: 'Revenue', zone: 'values' }],
      500,
    );
    expect(query!.limit).toBe(500);
  });

  it('returns null when no measures assigned', () => {
    const query = buildSemanticQuery([
      { id: 'd1', name: 'Country', zone: 'rows' },
    ]);
    expect(query).toBeNull();
  });

  it('handles empty items array', () => {
    const query = buildSemanticQuery([]);
    expect(query).toBeNull();
  });

  it('preserves row and column dimensions', () => {
    const query = buildSemanticQuery([
      { id: 'm1', name: 'Revenue', zone: 'values' },
      { id: 'd1', name: 'Product', zone: 'rows' },
      { id: 'd2', name: 'Region', zone: 'rows' },
      { id: 'd3', name: 'Quarter', zone: 'columns' },
    ]);
    expect(query!.dimensions).toEqual(expect.arrayContaining(['d1', 'd2', 'd3']));
    expect(query!.dimensions).toHaveLength(3);
  });
});
