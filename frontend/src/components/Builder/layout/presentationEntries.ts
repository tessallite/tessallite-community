/**
 * Merging one change into one saved presentation entry (R10).
 *
 * The rule these exist to enforce, from spec §5: "never silently drop unknown
 * presentation fields". A saved layout can carry fields this build does not
 * know about — written by a newer build, or by a feature added since — and a
 * drag must not delete them.
 *
 * That is easy to get wrong in the obvious way: rebuilding the entry and
 * enumerating the fields you care about. It reads as careful and it silently
 * discards everything you did not list. The drag write site did exactly that,
 * enumerating only `w` and `h`, which deleted `pinned` on the first drag of a
 * pinned table.
 *
 * So every writer merges through here instead. A field is only ever removed by
 * naming it explicitly with an `undefined` value, which is how the redraw path
 * clears a superseded legacy `waypoint`.
 */
import type { CanvasLayout } from "../../../api/types_domains/connections_models";

export type TableEntry = NonNullable<CanvasLayout["tables"]>[string];
export type EdgeEntry = NonNullable<CanvasLayout["edges"]>[string];

/**
 * Merge a change into a table's saved entry.
 *
 * A table entry must always have coordinates, so an entry that did not exist
 * starts at the origin; the caller supplies the real position in `change`.
 */
export function mergeTableEntry(previous: TableEntry | undefined, change: Partial<TableEntry>): TableEntry {
  return { ...(previous ?? { x: 0, y: 0 }), ...change };
}

/** Merge a change into a relationship's saved entry. */
export function mergeEdgeEntry(previous: EdgeEntry | undefined, change: Partial<EdgeEntry>): EdgeEntry {
  return { ...(previous ?? {}), ...change };
}
