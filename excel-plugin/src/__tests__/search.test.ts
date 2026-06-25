import { describe, it, expect } from 'vitest';

interface Searchable {
  display_name: string;
  name: string;
  effective_description?: string;
  description?: string;
  display_folder?: string;
}

function filterBySearch(
  items: Searchable[],
  query: string,
  glossarySynonyms?: Map<string, string[]>,
  aliasLookup?: Map<string, string[]>,
): Searchable[] {
  if (!query.trim()) return items;
  const q = query.toLowerCase();
  return items.filter(item => {
    if (
      item.display_name.toLowerCase().includes(q) ||
      item.name.toLowerCase().includes(q) ||
      item.effective_description?.toLowerCase().includes(q) ||
      item.description?.toLowerCase().includes(q) ||
      item.display_folder?.toLowerCase().includes(q)
    ) return true;
    const syns = glossarySynonyms?.get(item.display_name);
    if (syns?.some(s => s.includes(q))) return true;
    const aliases = aliasLookup?.get(item.name.toLowerCase()) ?? aliasLookup?.get(item.display_name.toLowerCase());
    if (aliases?.some(a => a.includes(q))) return true;
    return false;
  });
}

const items: Searchable[] = [
  { display_name: 'Revenue', name: 'revenue_amount', effective_description: 'Total sales revenue', description: 'Sum of all sales', display_folder: 'Finance' },
  { display_name: 'Cost', name: 'cost_amount', description: 'Total cost of goods' },
  { display_name: 'Customer Count', name: 'customer_count' },
];

describe('search filtering', () => {
  it('matches exact display_name', () => {
    const result = filterBySearch(items, 'Revenue');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('revenue_amount');
  });

  it('matches partial display_name', () => {
    const result = filterBySearch(items, 'Rev');
    expect(result).toHaveLength(1);
  });

  it('matches on technical name', () => {
    const result = filterBySearch(items, 'revenue_amount');
    expect(result).toHaveLength(1);
  });

  it('matches on description text', () => {
    const result = filterBySearch(items, 'Total sales');
    expect(result).toHaveLength(1);
  });

  it('matches on display_folder', () => {
    const result = filterBySearch(items, 'Finance');
    expect(result).toHaveLength(1);
  });

  it('is case-insensitive', () => {
    const result = filterBySearch(items, 'REVENUE');
    expect(result).toHaveLength(1);
  });

  it('matches on glossary synonym', () => {
    const synonyms = new Map<string, string[]>();
    synonyms.set('Revenue', ['sales', 'income']);
    const result = filterBySearch(items, 'sales', synonyms);
    expect(result).toHaveLength(1);
    expect(result[0].display_name).toBe('Revenue');
  });

  it('matches on alias map', () => {
    const aliases = new Map<string, string[]>();
    aliases.set('customer_count', ['total_customers', 'client_count']);
    const result = filterBySearch(items, 'total_customers', undefined, aliases);
    expect(result).toHaveLength(1);
    expect(result[0].display_name).toBe('Customer Count');
  });

  it('returns empty array when no match', () => {
    const result = filterBySearch(items, 'nonexistent');
    expect(result).toHaveLength(0);
  });

  it('returns all items when query is empty', () => {
    const result = filterBySearch(items, '');
    expect(result).toHaveLength(3);
  });
});
