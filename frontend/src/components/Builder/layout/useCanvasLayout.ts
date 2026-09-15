/**
 * Canvas layout orchestration: worker lifecycle, apply guards, busy/error state.
 *
 * Keeps `Canvas.tsx` wiring small and keeps every "is this result still valid?"
 * decision in one place.
 *
 * Staleness is guarded by a *content signature* plus a monotonic revision rather
 * than by a handful of call sites. Any layout-relevant edit — a drag, a resize,
 * a join change, an undo, a manual bend, a model switch — changes the signature
 * by construction, so a worker result computed against an older signature can
 * never be applied. A missed call site cannot silently reopen that hole.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { LayoutClient, LayoutError, type LayoutClientLike } from "./layoutClient";
import type {
  LayoutFailureCode, LayoutOperation, LayoutOptions, LayoutResult, LayoutSnapshot, PathMode } from "./types";

export interface SignatureNode {
  id: string;
  position: { x: number; y: number };
  /** React Flow reports an unmeasured dimension as `null`, not `undefined`. */
  width?: number | null;
  height?: number | null;
  measured?: { width?: number | null; height?: number | null } | null;
  /** Carries `pinned`. A protection change must supersede an in-flight batch. */
  data?: unknown;
}

export interface SignatureEdge {
  id: string;
  source: string;
  target: string;
  data?: unknown;
}

/**
 * Whether this card is protected from automatic placement.
 *
 * Kept in the staleness signature because protection decides what a result is
 * ALLOWED to move, not merely where things are. Omitting it let this sequence
 * through: unlock a table, start Arrange, Undo the unlock before the result
 * lands. The card is protected again, the signature never changed, and the
 * result computed while it was movable still applied and moved it.
 */
function nodeProtectionFingerprint(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  return (data as { pinned?: unknown }).pinned === true ? "pinned" : "";
}

function edgeRouteFingerprint(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  const record = data as Record<string, unknown>;
  const waypoints = Array.isArray(record.waypoints)
    ? (record.waypoints as Array<{ x?: number; y?: number }>).map((point) => `${point?.x},${point?.y}`).join(";")
    : record.waypoint
      ? `${(record.waypoint as { x?: number }).x},${(record.waypoint as { y?: number }).y}`
      : "";
  return [
    waypoints,
    record.sourceSide ?? "",
    record.targetSide ?? "",
    record.sourceRatio ?? "",
    record.targetRatio ?? "",
    record.pathing ?? "",
    // Protection and provenance: a locked route may not be re-routed, and a
    // manual route is not replaced. A result computed before either changed is
    // about a different problem.
    record.locked === true ? "locked" : "",
    record.routeMode ?? "",
    // Marker heels move the docking points, so a notation change moves every
    // terminal even though no card moved.
    record.sourceMarkerExtent ?? "",
    record.targetMarkerExtent ?? "",
  ].join("|");
}

/** Cheap, order-stable fingerprint of everything a layout result depends on. */
export function layoutSignature(nodes: SignatureNode[], edges: SignatureEdge[]): string {
  const nodePart = nodes
    .map((node) => {
      const width = node.measured?.width ?? node.width ?? "";
      const height = node.measured?.height ?? node.height ?? "";
      return `${node.id}@${node.position.x},${node.position.y}:${width}x${height}:${nodeProtectionFingerprint(node.data)}`;
    })
    .join(";");
  const edgePart = edges
    .map((edge) => `${edge.id}:${edge.source}->${edge.target}:${edgeRouteFingerprint(edge.data)}`)
    .join(";");
  return `${nodePart}##${edgePart}`;
}

/**
 * Signature of a real user edit: identity, position and route geometry. This is what
 * supersedes an in-flight batch.
 *
 * Card measurement is deliberately excluded and handled separately by the geometry
 * signature: React Flow publishes `measured` sizes asynchronously after mount, so a
 * batch spanning a measurement update must not be discarded as stale — but the result is
 * still validated against the effective geometry before it may touch the canvas (see
 * layoutGeometrySignature and the apply-time policy in run()).
 */
export function layoutStabilitySignature(nodes: SignatureNode[], edges: SignatureEdge[]): string {
  const nodePart = nodes
    .map((node) => `${node.id}@${node.position.x},${node.position.y}:${nodeProtectionFingerprint(node.data)}`)
    .join(";");
  const edgePart = edges
    .map((edge) => `${edge.id}:${edge.source}->${edge.target}:${edgeRouteFingerprint(edge.data)}`)
    .join(";");
  return `${nodePart}##${edgePart}`;
}

/**
 * Signature of the effective card geometry a layout result depends on: resolved card
 * sizes (and, through them, the attachment-point geometry the renderer derives). This
 * is the part React Flow's measurement pass may republish without any user edit.
 *
 * Apply-time policy: a stability change (identity, position, route geometry) supersedes
 * the batch outright; a geometry-only change rejects the result before the canvas or
 * persistence is touched and allows exactly one automatic recomputation against the
 * updated snapshot.
 */
export function layoutGeometrySignature(nodes: SignatureNode[], edges: SignatureEdge[]): string {
  const nodePart = nodes
    .map((node) => {
      const width = node.measured?.width ?? node.width ?? "";
      const height = node.measured?.height ?? node.height ?? "";
      return `${node.id}:${width}x${height}`;
    })
    .join(";");
  return nodePart;
}

/**
 * Per-batch inputs that are not part of the persisted presentation contract.
 */
export interface LayoutRunContext {
  /**
   * Movable set for this batch. Nodes outside it are presented to the worker as
   * pinned, so `arrange-all` moves only them. Used for placing genuinely new
   * tables without persisting a pin and without a second placement system.
   */
  movableIds?: Set<string>;
}

/**
 * Why a layout batch did not apply.
 *
 * The caller has to tell two situations apart, because the correct response is
 * opposite in each. `geometry-invalid` and `no-route` mean the engine looked at
 * the requested geometry and reported that it cannot be drawn — evidence the
 * edit is bad. Everything else (engine unavailable, timeout, cancellation, a
 * newer batch superseding this one) means the engine never rendered a verdict,
 * so discarding the user's edit on that basis would be throwing away work for
 * an infrastructure hiccup.
 */
export interface LayoutRunOutcome {
  applied: boolean;
  failure?: LayoutFailureCode;
}

/** A run that was overtaken or never started: it owns nothing and reports nothing. */
const SUPERSEDED: LayoutRunOutcome = { applied: false };

export interface UseCanvasLayoutOptions {
  projectId: string;
  modelId: string;
  nodes: SignatureNode[];
  edges: SignatureEdge[];
  readOnly: boolean;
  /** Built at request time from the same render the revision was captured for. */
  buildSnapshot: (
    revision: number,
    options: Partial<LayoutOptions>,
    context: LayoutRunContext,
  ) => LayoutSnapshot;
  /** Called only for a current, successful, permitted result. */
  applyResult: (result: LayoutResult) => void;
  /** Human-readable message for a failed or refused layout. */
  onError: (message: string) => void;
  createClient?: () => LayoutClientLike;
  /**
   * Global pathing preference the renderer falls back to for edges without an
   * explicit override. Folded into the staleness signature because it changes
   * computed route geometry (straight vs orthogonal), so a result computed under
   * the previous global preference must not be applied after it changes.
   */
  relationPathing?: PathMode;
}

export interface CanvasLayoutController {
  /**
   * Runs one batch. Reports whether the result was applied AND, when it was
   * not, why — the caller's correct response differs between "the engine says
   * this geometry cannot be drawn" and "the engine never answered".
   */
  run: (
    operation: LayoutOperation,
    options?: Partial<LayoutOptions>,
    context?: LayoutRunContext,
  ) => Promise<LayoutRunOutcome>;
  /** Abandon the active batch (Cancel button). */
  cancel: () => void;
  /** Retry the last failed batch. */
  retry: () => Promise<LayoutRunOutcome>;
  busy: boolean;
  /** True when a batch failed and can be retried. */
  canRetry: boolean;
  revision: number;
}

export function useCanvasLayout(options: UseCanvasLayoutOptions): CanvasLayoutController {
  const {
    projectId,
    modelId,
    nodes,
    edges,
    readOnly,
    buildSnapshot,
    applyResult,
    onError,
    createClient,
    relationPathing,
  } = options;

  const clientRef = useRef<LayoutClientLike | null>(null);
  const createClientRef = useRef(createClient);
  createClientRef.current = createClient;
  const [busy, setBusy] = useState(false);
  const [canRetry, setCanRetry] = useState(false);
  const [revision, setRevision] = useState(0);
  const revisionRef = useRef(0);
  // The effective path mode changes computed route geometry, so it belongs in
  // the content signature: a global pathing flip must supersede an in-flight
  // batch exactly like a manual bend does (F08).
  const pathingFingerprint = `pathing=${relationPathing ?? "orthogonal"}`;
  const signature = `${layoutSignature(nodes, edges)}##${pathingFingerprint}`;
  const signatureRef = useRef(signature);
  const stabilitySignature = `${layoutStabilitySignature(nodes, edges)}##${pathingFingerprint}`;
  const stabilityRef = useRef(stabilitySignature);
  const geometrySignature = layoutGeometrySignature(nodes, edges);
  const geometryRef = useRef(geometrySignature);
  const lastRef = useRef<{ operation: LayoutOperation; options: Partial<LayoutOptions>; context: LayoutRunContext } | null>(null);
  const mountedRef = useRef(true);
  // Guards read the newest values without re-creating callbacks mid-batch.
  const readOnlyRef = useRef(readOnly);
  const applyRef = useRef(applyResult);
  const errorRef = useRef(onError);
  const scopeRef = useRef({ projectId, modelId });

  readOnlyRef.current = readOnly;
  applyRef.current = applyResult;
  errorRef.current = onError;
  scopeRef.current = { projectId, modelId };

  // Symmetric lifecycle: the client is created in effect setup and disposed in
  // the matching cleanup. React StrictMode's development-only setup/cleanup/
  // setup replay must recreate the client, not leave a disposed one referenced.
  useEffect(() => {
    const client = createClientRef.current ? createClientRef.current() : new LayoutClient();
    clientRef.current = client;
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (clientRef.current === client) {
        clientRef.current = null;
      }
      client.dispose();
    };
  }, []);

  // Any layout-relevant edit advances the revision, so an in-flight batch
  // computed against the previous content is refused at apply time.
  if (signatureRef.current !== signature) {
    signatureRef.current = signature;
    revisionRef.current += 1;
  }
  stabilityRef.current = stabilitySignature;
  geometryRef.current = geometrySignature;
  if (revision !== revisionRef.current) {
    setRevision(revisionRef.current);
  }

  /** Monotonic identity of the run that currently owns the controller state. */
  const runSeqRef = useRef(0);


  const run = useCallback(
    async (
      operation: LayoutOperation,
      layoutOptions: Partial<LayoutOptions> = {},
      context: LayoutRunContext = {},
    ): Promise<LayoutRunOutcome> => {
      const client = clientRef.current;
      if (!client) return { applied: false, failure: "engine-unavailable" };
      if (readOnlyRef.current) {
        errorRef.current("layout changes are unavailable in a read-only session");
        return { applied: false, failure: "invalid-input" };
      }
      // A model switch invalidates any queued work from the previous model.
      const scope = { ...scopeRef.current };
      // Every run owns an identity. Without one, an OLDER run finishing after a
      // newer one started would clear the newer one's busy state, surface its
      // own stale error over the newer work, and offer a Retry that replays the
      // superseded operation. Cancelling the request is not enough: the
      // completion-side effects have to be ignored too.
      const runId = runSeqRef.current + 1;
      runSeqRef.current = runId;
      const isLiveRun = () => runSeqRef.current === runId;
      lastRef.current = { operation, options: layoutOptions, context };
      setBusy(true);
      setCanRetry(false);
      let recomputed = false;
      try {
        // One bounded loop. Passive measurement settling may trigger exactly one
        // automatic recomputation against the updated snapshot; a second geometry
        // change means the geometry will not settle, so the request ends visibly.
        for (;;) {
          const startedRevision = revisionRef.current;
          const startedStability = stabilityRef.current;
          const startedGeometry = geometryRef.current;
          const snapshot = buildSnapshot(startedRevision, layoutOptions, context);
          const result = await client.request(snapshot, operation);
          // Re-check every precondition *after* the await: the batch is only
          // applied if nothing changed while the engine was working.
          if (!mountedRef.current) return SUPERSEDED;
          if (readOnlyRef.current) return SUPERSEDED;
          // Superseded by a newer run: this one may not apply, report or retry.
          if (!isLiveRun()) return SUPERSEDED;
          if (scopeRef.current.projectId !== scope.projectId || scopeRef.current.modelId !== scope.modelId) return SUPERSEDED;
          // A real edit — identity, position or route geometry — supersedes this
          // operation. Never restart it over the user's newer intent.
          if (stabilityRef.current !== startedStability) return SUPERSEDED;
          // Effective geometry changed while the engine worked. Reject the result
          // BEFORE touching the canvas or persistence; allow exactly one
          // recomputation against the updated snapshot.
          if (geometryRef.current !== startedGeometry) {
            if (recomputed) {
              setCanRetry(true);
              errorRef.current("layout cancelled: card geometry kept changing. Please try again.");
              return { applied: false, failure: "cancelled" };
            }
            recomputed = true;
            continue;
          }
          applyRef.current(result);
          return { applied: true };
        }
      } catch (error) {
        if (!mountedRef.current) return SUPERSEDED;
        const code: LayoutFailureCode = error instanceof LayoutError ? error.code : "unknown";
        if (error instanceof LayoutError && error.code === "cancelled") return { applied: false, failure: "cancelled" };
        // A superseded run's failure is not the user's current problem.
        if (!isLiveRun()) return SUPERSEDED;
        setCanRetry(true);
        errorRef.current(error instanceof Error ? error.message : String(error));
        return { applied: false, failure: code };
      } finally {
        // Only the live run owns the busy state.
        if (mountedRef.current && isLiveRun()) setBusy(false);
      }
    },
    [buildSnapshot],
  );

  const cancel = useCallback(() => {
    // Retire the live run's identity as well as cancelling the request, so a
    // reply already in flight cannot apply or re-assert busy after the user
    // has cancelled.
    runSeqRef.current += 1;
    clientRef.current?.cancel("user cancelled the layout");
    setBusy(false);
  }, []);

  const retry = useCallback(async (): Promise<LayoutRunOutcome> => {
    const last = lastRef.current;
    if (!last) return SUPERSEDED;
    return run(last.operation, last.options, last.context);
  }, [run]);

  return { run, cancel, retry, busy, canRetry, revision };
}
