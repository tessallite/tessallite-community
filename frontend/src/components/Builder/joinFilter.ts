import type { Join } from "../../api/types_domains/dimensions";

/**
 * Split joins into those whose endpoints are both present on the canvas and
 * those that are not.
 *
 * The ERD canvas loads tables via the `/sources/{id}/tables` endpoint, which
 * intentionally omits model tables linked to an autocreated calendar table
 * (see model-service `tables.list_tables`). Joins are unfiltered, so a join to
 * one of those hidden calendar tables references a `table_id` that is not a
 * canvas node. Feeding such an edge into the d3-force `forceLink` throws
 * "node not found: undefined" and white-screens the whole builder, and an edge
 * to a table that isn't drawn cannot be drawn either.
 *
 * @param joins   all joins on the model
 * @param nodeIds the set of table ids currently rendered on the canvas
 */
export function partitionJoinsByEndpoints(
  joins: Join[],
  nodeIds: Set<string>,
): { linked: Join[]; dropped: Join[] } {
  const linked: Join[] = [];
  const dropped: Join[] = [];
  for (const j of joins) {
    if (nodeIds.has(j.left_table_id) && nodeIds.has(j.right_table_id)) {
      linked.push(j);
    } else {
      dropped.push(j);
    }
  }
  return { linked, dropped };
}

/**
 * Number of persisted joins that cannot be drawn with the current canvas
 * catalogue. The canvas uses this to surface the omission to the modeller;
 * silently logging it to the console made calendar joins look like data loss.
 */
export function countDroppedJoins(joins: Join[], nodeIds: Set<string>): number {
  return partitionJoinsByEndpoints(joins, nodeIds).dropped.length;
}
