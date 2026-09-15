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
import type { SemanticQuery } from '../types/tessallite';
/**
 * Normalize an agent-service semantic_query dict into the plugin's
 * SemanticQuery type, preserving where/having as filters and sort as order.
 *
 * If the input already uses the plugin schema (has `filters` / `order`),
 * those are kept as-is. Both schemas' fields are merged so the function
 * is idempotent and tolerant of mixed shapes.
 */
export declare function normalizeAgentSemanticQuery(raw: unknown): SemanticQuery;
