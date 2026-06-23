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

function typeHasQueryable(objIds: string[], allow: Set<string>): { count: number; anyQueryable: boolean } {
  if (objIds.length === 0) return { count: 0, anyQueryable: false };
  if (allow.size === 0) return { count: objIds.length, anyQueryable: true };
  return { count: objIds.length, anyQueryable: objIds.some((id) => allow.has(id)) };
}

/**
 * Returns the set of table ids that should be dimmed in the persona overlay.
 * When all three allow lists are empty the persona is unrestricted and nothing
 * is dimmed.
 */
export function computeDimmedTableIds(
  tables: TableObjects[],
  allow: PersonaAllowLists,
): Set<string> {
  const result = new Set<string>();
  const unrestricted =
    allow.measureIds.size === 0 &&
    allow.dimensionIds.size === 0 &&
    allow.hierarchyIds.size === 0;
  if (unrestricted) return result;

  for (const t of tables) {
    const m = typeHasQueryable(t.measureIds, allow.measureIds);
    const d = typeHasQueryable(t.dimensionIds, allow.dimensionIds);
    const h = typeHasQueryable(t.hierarchyIds, allow.hierarchyIds);
    const totalObjects = m.count + d.count + h.count;
    if (totalObjects === 0) continue; // no objects -> nothing to exclude
    if (!(m.anyQueryable || d.anyQueryable || h.anyQueryable)) {
      result.add(t.tableId);
    }
  }
  return result;
}
