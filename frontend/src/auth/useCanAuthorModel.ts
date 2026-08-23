/**
 * Single source of truth for "may the current user author (mutate) the open
 * model in the Model Builder?" (F-026-04).
 *
 * Bug-8784: previously this composed ``canEditModelConfig()`` — a COARSE
 * local-role check (``currentUserRole()``) — with ``readOnly``.  But
 * ``modeler`` is never a ``LocalUser.role`` value; it is a per-project
 * ``UserAccessBinding`` resolved by the backend.  The canonical
 * locally-provisioned modeller (local role ``member`` + project ``modeler``
 * binding) was therefore locked out of every authoring panel.
 *
 * The correct signal already exists: ``ModelBuilder.tsx`` reads
 * ``caller_can_author`` from the model-detail response and sets the
 * builder-store ``readOnly`` flag.  ``Canvas`` already gates on it.  This
 * hook now derives its answer from that single authoritative source.
 *
 * ``canEditModelConfig()`` still exists for non-Model-Builder surfaces
 * (e.g. Explorer privilege checks) where no per-model binding is loaded.
 */
import { useBuilderStore } from "../store/builderStore";

export function useCanAuthorModel(): boolean {
  return !useBuilderStore((s) => s.readOnly);
}
