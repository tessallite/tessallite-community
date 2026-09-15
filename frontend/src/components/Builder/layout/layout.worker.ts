import config from "./config.json";
import {
  defaultOptions,
  layoutRadial,
  metricsForLayout,
  placeBlockAroundOriginal,
  separateRectangles,
  validateLayoutResult,
  validateSnapshot,
} from "./geometry";
import { layoutWithElk, spacingForPreset } from "./elkLayout";
// The movable-table rule lives in its own pure module because the canvas reads
// it too, to decide whether Arrange selected can be offered at all.
import { movableIdsFor } from "./movableSet";
import { routeEdges } from "./routeEdges";
import type {
  LayoutFailure,
  LayoutNodeSnapshot,
  LayoutOperation,
  LayoutResult,
  LayoutSnapshot,
  LayoutWorkerMessage,
  Point,
} from "./types";
import { failureCodeOf } from "./layoutErrors";

const cancelled = new Set<string>();

function failure(snapshot: LayoutSnapshot, code: LayoutFailure["code"], message: string): LayoutFailure {
  return { kind: "failure", scope: snapshot.scope, revision: snapshot.revision, code, message };
}

function currentPositions(nodes: LayoutNodeSnapshot[]): Record<string, Point> {
  return Object.fromEntries(nodes.map((node) => [node.id, { x: node.x, y: node.y }]));
}

function checkCancelled(requestId: string): void {
  if (cancelled.has(requestId)) {
    cancelled.delete(requestId);
    throw Object.assign(new Error("layout was cancelled"), { code: "cancelled" });
  }
}

function elapsedSince(started: number): number {
  return (typeof performance !== "undefined" ? performance.now() : Date.now()) - started;
}

/**
 * One layout batch. Position ownership is explicit per operation:
 *   - `reroute-links` never changes a table coordinate (R05), so it skips
 *     placement and separation entirely, and route validation does not assert
 *     card non-overlap because the operation does not own the cards.
 *   - `arrange-all` / `arrange-selected` move only their movable set; every
 *     other card is an immovable separation obstacle (R04).
 */
export async function computeLayout(
  snapshot: LayoutSnapshot,
  operation: LayoutOperation,
  requestId = "test",
): Promise<LayoutResult> {
  const started = typeof performance !== "undefined" ? performance.now() : Date.now();
  validateSnapshot(snapshot);
  checkCancelled(requestId);

  const nodeGap = Number(config.routing.nodeGap);
  const options = defaultOptions(snapshot.options);
  const movableIds = movableIdsFor(snapshot.nodes, snapshot.edges, operation);
  // `reroute-links` and `repair-routes` never own table coordinates (R05):
  // they skip placement and separation entirely, and route validation does not
  // assert card non-overlap because the operation does not own the cards.
  const ownsPositions = operation !== "reroute-links" && operation !== "repair-routes";
  // `repair-routes` recomputes only engine-generated auto routes after a card
  // move/resize; user-authored manual geometry stays put (F05).
  const retainManual = operation === "repair-routes";

  if (operation === "arrange-selected" && movableIds.size === 0) {
    throw new Error("select at least one movable table before arranging the selection");
  }

  let positions = currentPositions(snapshot.nodes);

  if (ownsPositions && movableIds.size > 0) {
    const spacing = spacingForPreset(options.preset, options.spacing);
    const engineOptions = { ...spacing, direction: options.direction };
    // Radial is a measured, fact-centred placement stage inside the worker,
    // followed by the same separation and coordinated routing as every other
    // preset (F04). Hierarchical and Compact both use ELK Layered.
    const proposed = options.preset === "radial"
      ? layoutRadial(snapshot.nodes, snapshot.edges, movableIds, nodeGap)
      : (await layoutWithElk(snapshot, movableIds, engineOptions)).positions;
    checkCancelled(requestId);
    const movableNodes = snapshot.nodes.filter((node) => movableIds.has(node.id));
    const translated = placeBlockAroundOriginal(
      movableNodes,
      proposed,
      snapshot.nodes,
      positions,
      nodeGap,
      Number(config.routing.maxCandidateDistance),
    );
    positions = { ...positions, ...translated };
  }
  checkCancelled(requestId);

  if (ownsPositions) {
    const immovable = new Set(snapshot.nodes.filter((node) => !movableIds.has(node.id)).map((node) => node.id));
    positions = separateRectangles(snapshot.nodes, positions, immovable, nodeGap);
  }
  checkCancelled(requestId);

  const routeOptions = {
    endpointDriftLimitPx: Number(config.routing.endpointDriftLimitPx),
    shapeBufferDistance: Number(config.routing.shapeBufferDistance),
    idealNudgingDistance: Number(config.routing.idealNudgingDistance),
    segmentPenalty: Number(config.routing.segmentPenalty),
    anglePenalty: Number(config.routing.anglePenalty),
    crossingPenalty: Number(config.routing.crossingPenalty),
    sharedPathPenalty: Number(config.routing.sharedPathPenalty),
  };
  const routes = await routeEdges(snapshot.nodes, snapshot.edges, positions, routeOptions, ownsPositions, retainManual);
  checkCancelled(requestId);
  // Best reasonable effort (product decision, Bug-10032): a relationship the
  // router could not steer around every card does not make the whole batch
  // fail. The diagram is produced and the crossings are counted in
  // `metrics.throughNodeSegmentCount`, which the canvas surfaces. Refusing
  // instead left a modeller with a densely connected model pressing Arrange
  // and getting nothing at all. Every other invariant here stays fail-closed.
  validateLayoutResult(snapshot, positions, routes, nodeGap, ownsPositions, false);

  return {
    kind: "success",
    scope: snapshot.scope,
    revision: snapshot.revision,
    positions,
    routes,
    options,
    metrics: metricsForLayout(snapshot.nodes, snapshot.edges, positions, routes, elapsedSince(started), nodeGap),
  };
}

function post(response: LayoutResult | LayoutFailure): void {
  const scope = globalThis as unknown as { postMessage(message: LayoutResult | LayoutFailure): void };
  scope.postMessage(response);
}

const workerScope = globalThis as unknown as {
  onmessage: ((event: MessageEvent<LayoutWorkerMessage>) => void) | null;
};

workerScope.onmessage = (event: MessageEvent<LayoutWorkerMessage>) => {
  const message = event.data;
  if (message.type === "cancel") {
    cancelled.add(message.requestId);
    return;
  }
  void computeLayout(message.snapshot, message.operation, message.requestId)
    .then(post)
    .catch((error: unknown) => {
      // The code is established where the rejection is established, not
      // recovered here from an English message. Anything that still arrives
      // unclassified stays `unknown`, and the canvas must treat `unknown` as
      // "the engine failed", never as "the edit is safe to keep".
      post(failure(message.snapshot, failureCodeOf(error), error instanceof Error ? error.message : String(error)));
    });
};
