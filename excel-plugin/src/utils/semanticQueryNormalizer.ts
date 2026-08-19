/**
 * Bug-7392: Normalize agent-service semantic_query to the plugin's
 * SemanticQuery shape for safe re-fetch.
 *
 * The agent-service produces semantic_query with:
 *   - where:  [{name, op, value}]       (agent tool-call schema)
 *   - having: [{name, op, value}]
 *   - sort:   [{name, direction}]
 *   - limit:  number
 *
 * The plugin's SemanticQuery (types/tessallite.ts) expects:
 *   - filters: [{dimension, operator, values}]
 *   - order:   Record<string, 'asc' | 'desc'>
 *   - limit:   number
 *
 * And executeQuery (api/queryRouter.ts) maps filters.dimension -> dimension,
 * filters.operator -> operator, and order -> order_by for the wire format.
 *
 * Without normalization the cast `turn.semantic_query as SemanticQuery`
 * silently drops where/having/sort, causing the re-fetch to POST
 * measures + dimensions ONLY -- inserting UNFILTERED numbers labeled
 * as the filtered query (WRONG NUMBERS).
 */
import type { SemanticQuery, QueryFilter } from '../types/tessallite';

/**
 * A single agent-side where/having filter entry.
 * Shape: {name: string, op: string, value: scalar | scalar[]}.
 */
interface AgentFilter {
  name: string;
  op: string;
  value?: unknown;
}

/**
 * A single agent-side sort entry.
 * Shape: {name: string, direction: 'asc' | 'desc'}.
 */
interface AgentSortEntry {
  name: string;
  direction?: string;
}

/**
 * Convert an agent-side filter entry ({name, op, value}) to the plugin's
 * QueryFilter ({dimension, operator, values}).
 */
function convertFilter(f: AgentFilter): QueryFilter {
  const values: string[] = [];
  if (f.value !== undefined && f.value !== null) {
    if (Array.isArray(f.value)) {
      values.push(...f.value.map(String));
    } else {
      values.push(String(f.value));
    }
  }
  return {
    dimension: f.name,
    operator: f.op,
    values: values.length > 0 ? values : undefined,
  };
}

/**
 * Normalize an agent-service semantic_query dict into the plugin's
 * SemanticQuery type, preserving where/having as filters and sort as order.
 *
 * If the input already uses the plugin schema (has `filters` / `order`),
 * those are kept as-is. Both schemas' fields are merged so the function
 * is idempotent and tolerant of mixed shapes.
 */
export function normalizeAgentSemanticQuery(raw: unknown): SemanticQuery {
  if (!raw || typeof raw !== 'object') {
    return { measures: [], dimensions: [] };
  }

  const obj = raw as Record<string, unknown>;

  // --- measures / dimensions (shared field names) ---
  const measures = Array.isArray(obj.measures)
    ? (obj.measures as string[])
    : [];
  const dimensions = Array.isArray(obj.dimensions)
    ? (obj.dimensions as string[])
    : [];

  // --- filters ---
  // Prefer existing `filters` if present (plugin schema); fall back to
  // agent's `where` + `having` merged.
  let filters: QueryFilter[] | undefined;
  if (Array.isArray(obj.filters) && obj.filters.length > 0) {
    filters = obj.filters as QueryFilter[];
  } else {
    const merged: QueryFilter[] = [];
    if (Array.isArray(obj.where)) {
      for (const f of obj.where) {
        if (f && typeof f === 'object' && 'name' in f && 'op' in f) {
          merged.push(convertFilter(f as AgentFilter));
        }
      }
    }
    if (Array.isArray(obj.having)) {
      for (const f of obj.having) {
        if (f && typeof f === 'object' && 'name' in f && 'op' in f) {
          merged.push(convertFilter(f as AgentFilter));
        }
      }
    }
    if (merged.length > 0) {
      filters = merged;
    }
  }

  // --- order ---
  // Prefer existing `order` if present (plugin schema); fall back to
  // agent's `sort` array.
  let order: Record<string, 'asc' | 'desc'> | undefined;
  if (obj.order && typeof obj.order === 'object' && !Array.isArray(obj.order)) {
    order = obj.order as Record<string, 'asc' | 'desc'>;
  } else if (Array.isArray(obj.sort)) {
    const converted: Record<string, 'asc' | 'desc'> = {};
    for (const s of obj.sort) {
      if (s && typeof s === 'object' && 'name' in s) {
        const entry = s as AgentSortEntry;
        const dir = entry.direction === 'asc' ? 'asc' : 'desc';
        converted[entry.name] = dir;
      }
    }
    if (Object.keys(converted).length > 0) {
      order = converted;
    }
  }

  // --- limit ---
  const limit = typeof obj.limit === 'number' ? obj.limit : undefined;

  return {
    measures,
    dimensions,
    filters,
    order,
    limit,
  };
}
