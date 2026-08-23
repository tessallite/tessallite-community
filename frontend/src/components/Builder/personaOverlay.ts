/**
 * Persona preview overlay — which canvas tables to dim.
 *
 * Mirrors the backend persona gate
 * (services/query-router/src/security/persona_gate.py): a per-type EMPTY allow
 * list means "unrestricted for that type" (every object of that type is
 * queryable). So an object is queryable iff its type's allow list is empty, or
 * the object id is in it. A table is dimmed only when it carries at least one
 * semantic object AND none of those objects are queryable under the persona.
 *
 * The previous inline formula wrongly dimmed tables whose objects were all of
 * an unrestricted type (e.g. a measures-only persona greyed every dimension
 * table). This pure helper makes that rule testable in isolation (F-026-06).
 *
 * F-008-03: the preview also reflects COLUMN-LEVEL security. A measure or
 * dimension whose backing source column is in the persona's
 * ``restricted_column_ids`` is NOT queryable for the persona (the runtime CLS
 * gate blocks it), so it must not count as a "queryable" object in the overlay,
 * and the specific restricted objects are surfaced so the canvas can mark them
 * — otherwise a modeller previews a persona and sees restricted fields as
 * available even though execution blocks them.
 */

export interface PersonaAllowLists {
  measureIds: Set<string>;
  dimensionIds: Set<string>;
  hierarchyIds: Set<string>;
}

export interface TableObjects {
  tableId: string;
  measureIds: string[];
  dimensionIds: string[];
  hierarchyIds: string[];
}

/**
 * Source-column backing for each semantic object, used to evaluate CLS.
 * ``null`` when the object has no single backing column (calculated / UDA):
 * the preview treats those as not-directly-restricted (the runtime transitive
 * closure still gates them; the overlay is a direct-column visual hint, not the
 * enforcement boundary).
 */
export interface ObjectSourceColumns {
  measureSourceColumnId: Record<string, string | null>;
  dimensionSourceColumnId: Record<string, string | null>;
}

function isClsRestricted(
  objId: string,
  sourceColById: Record<string, string | null>,
  restrictedColumnIds: Set<string>,
): boolean {
  if (restrictedColumnIds.size === 0) return false;
  const col = sourceColById[objId];
  return col != null && restrictedColumnIds.has(col);
}

function typeHasQueryable(
  objIds: string[],
  allow: Set<string>,
  sourceColById: Record<string, string | null>,
  restrictedColumnIds: Set<string>,
  blockedIds?: Set<string>,
): { count: number; anyQueryable: boolean } {
  if (objIds.length === 0) return { count: 0, anyQueryable: false };
  const queryable = objIds.filter((id) => {
    if (blockedIds?.has(id)) return false;
    if (isClsRestricted(id, sourceColById, restrictedColumnIds)) return false;
    return allow.size === 0 || allow.has(id);
  });
  return { count: objIds.length, anyQueryable: queryable.length > 0 };
}

/**
 * Returns the set of table ids that should be dimmed in the persona overlay.
 * When all three allow lists are empty AND there are no CLS restrictions the
 * persona is unrestricted and nothing is dimmed.
 */
export function computeDimmedTableIds(
  tables: TableObjects[],
  allow: PersonaAllowLists,
  cls?: {
    restrictedColumnIds: Set<string>;
    sources: ObjectSourceColumns;
    blockedMeasureIds?: Set<string>;
    blockedDimensionIds?: Set<string>;
  },
): Set<string> {
  const result = new Set<string>();
  const restrictedColumnIds = cls?.restrictedColumnIds ?? new Set<string>();
  const blockedMeasureIds = cls?.blockedMeasureIds ?? new Set<string>();
  const blockedDimensionIds = cls?.blockedDimensionIds ?? new Set<string>();
  const measureSrc = cls?.sources?.measureSourceColumnId ?? {};
  const dimensionSrc = cls?.sources?.dimensionSourceColumnId ?? {};
  const unrestricted =
    allow.measureIds.size === 0 &&
    allow.dimensionIds.size === 0 &&
    allow.hierarchyIds.size === 0 &&
    restrictedColumnIds.size === 0 &&
    blockedMeasureIds.size === 0 &&
    blockedDimensionIds.size === 0;
  if (unrestricted) return result;

  for (const t of tables) {
    const m = typeHasQueryable(
      t.measureIds, allow.measureIds, measureSrc, restrictedColumnIds, blockedMeasureIds,
    );
    const d = typeHasQueryable(
      t.dimensionIds, allow.dimensionIds, dimensionSrc, restrictedColumnIds, blockedDimensionIds,
    );
    // Hierarchies have no single backing column; CLS does not gate them here.
    const h = typeHasQueryable(t.hierarchyIds, allow.hierarchyIds, {}, new Set<string>());
    const totalObjects = m.count + d.count + h.count;
    if (totalObjects === 0) continue; // no objects -> nothing to exclude
    if (!(m.anyQueryable || d.anyQueryable || h.anyQueryable)) {
      result.add(t.tableId);
    }
  }
  return result;
}

/**
 * Object ids (measures + dimensions) that are CLS-restricted for the persona —
 * their backing source column is in ``restrictedColumnIds``. The canvas marks
 * these as blocked in the persona preview (F-008-03).
 */
export function computeClsRestrictedObjectIds(
  sources: ObjectSourceColumns,
  restrictedColumnIds: Set<string>,
  serverBlocked?: { measureIds?: string[]; dimensionIds?: string[] },
): { measureIds: Set<string>; dimensionIds: Set<string> } {
  const measureIds = new Set<string>();
  const dimensionIds = new Set<string>();
  if (restrictedColumnIds.size > 0) {
    for (const [id, col] of Object.entries(sources.measureSourceColumnId)) {
      if (col != null && restrictedColumnIds.has(col)) measureIds.add(id);
    }
    for (const [id, col] of Object.entries(sources.dimensionSourceColumnId)) {
      if (col != null && restrictedColumnIds.has(col)) dimensionIds.add(id);
    }
  }
  // F-008-06: union the server CLS closure (calculated / UDA objects the
  // client cannot prove from source_column_id alone).
  for (const id of serverBlocked?.measureIds ?? []) measureIds.add(id);
  for (const id of serverBlocked?.dimensionIds ?? []) dimensionIds.add(id);
  return { measureIds, dimensionIds };
}

/**
 * Human-readable default-filter scope for the persona preview (F-008-03).
 * Each ``dimension = value`` (or ``in [...]`` / ``op value``) becomes one
 * chip string so a modeller sees the mandatory scope the persona applies —
 * the runtime AND-composes these with the user's filters (non-overridable).
 */
export function summarizeDefaultFilters(
  defaultFilters: Record<string, unknown> | null | undefined,
): string[] {
  if (!defaultFilters) return [];
  const out: string[] = [];
  for (const [dim, raw] of Object.entries(defaultFilters)) {
    if (dim.startsWith('@')) continue; // parameter override, not a filter
    if (Array.isArray(raw)) {
      out.push(`${dim} in [${raw.join(', ')}]`);
    } else if (raw != null && typeof raw === 'object') {
      const [op, val] = Object.entries(raw as Record<string, unknown>)[0] ?? [];
      if (op !== undefined) out.push(`${dim} ${op} ${String(val)}`);
    } else {
      out.push(`${dim} = ${String(raw)}`);
    }
  }
  return out;
}
