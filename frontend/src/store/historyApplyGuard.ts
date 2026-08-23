/**
 * Dependency-free re-entrancy guard for undo/redo history replays (F-026-02).
 *
 * While the canvas-history hook is replaying an inverse (undo) or forward
 * (redo) API write, the model-write response interceptor in api/client.ts must
 * NOT bump the editor store's content revision — the history hook sets the
 * reconciled revision directly to the value the model had at that history
 * position. Bumping it there would make undoing an edit back to the saved
 * baseline still read as dirty.
 *
 * This lives in its own module (no store / axios imports) so api/client.ts can
 * import `isApplyingHistory` synchronously at response time without creating a
 * store -> client -> store import cycle.
 */
let depth = 0;

export function beginHistoryApply(): void {
  depth += 1;
}

export function endHistoryApply(): void {
  depth = Math.max(0, depth - 1);
}

export function isApplyingHistory(): boolean {
  return depth > 0;
}
