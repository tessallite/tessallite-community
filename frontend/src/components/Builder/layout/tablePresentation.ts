/**
 * How a saved table entry becomes the card the user sees.
 *
 * There were three copies of this decision — gesture rollback, undo/redo, and
 * hydration on reopen — and they disagreed in ways the user could see:
 *
 *   - Undo restored the saved width and height into the persisted layout but
 *     left the card at its new size on screen, so the diagram and the file
 *     disagreed until the next reload.
 *   - Rollback of a FIRST resize only overwrote a dimension when the
 *     before-state already had one, so the explicit size the resize introduced
 *     survived the rollback that was supposed to remove it.
 *   - Hydration ignored any saved height of 160 or less, while the resizer's
 *     own minimum is 150. A card the user was allowed to make 150 tall
 *     reopened at a different height.
 *
 * One function now answers it for all of them.
 */

/** The smallest card the resizer will produce. Hydration must accept exactly this. */
export const MIN_TABLE_WIDTH = 200;
export const MIN_TABLE_HEIGHT = 150;

export interface TableSizeEntry {
  w?: number;
  h?: number;
}

/**
 * Is this a size the user could actually have produced?
 *
 * Rejects the artificially crushed values an earlier resizer defect wrote,
 * without rejecting a legitimately small card. The two rules were different
 * numbers in different files, which is what made a supported size look like
 * corrupt data.
 */
export function isSupportedTableSize(value: unknown, minimum: number): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= minimum;
}

/**
 * The node style expressing `entry`, starting from `currentStyle`.
 *
 * A dimension the entry does not carry is REMOVED, not left behind. That is the
 * whole point for a first-resize rollback: the before-state has no explicit
 * size because the card was content-sized, and restoring it has to give the
 * card back to the content, not keep the size the gesture introduced.
 */
export function styleForTableEntry(
  currentStyle: Record<string, unknown> | undefined,
  entry: TableSizeEntry | undefined,
): Record<string, unknown> {
  const style = { ...(currentStyle ?? {}) };

  if (isSupportedTableSize(entry?.w, MIN_TABLE_WIDTH)) style.width = entry!.w;
  else delete style.width;

  if (isSupportedTableSize(entry?.h, MIN_TABLE_HEIGHT)) style.height = entry!.h;
  else delete style.height;

  return style;
}
