import type {
  AnchorSide,
  LayoutEdgeSnapshot,
  LayoutNodeSnapshot,
  LayoutRoute,
  Point,
} from "./types";
import { compactPoints, rectForNode, routeHasDiagonal, routeTouchesUnrelatedNode, validateRoutes } from "./geometry";
import {
  ANCHOR_RATIO_MAX,
  ANCHOR_RATIO_MIN,
  adoptRoutedEndpoint,
  alignedSideRatios,
  clampAnchorRatio,
  honoursSavedDocking,
  oppositeSide,
  outwardVector,
  parallelOffsetFor,
  ratioWithParallelOffset,
  resolveDocking,
  sideFromCenters,
} from "./docking";
import { geometryInvalid, noRoute, engineUnavailable } from "./layoutErrors";

interface AvoidPoint {
  x: number;
  y: number;
}

interface AvoidPolyline {
  size(): number;
  at(index: number): AvoidPoint;
}

interface AvoidConnector {
  displayRoute(): AvoidPolyline;
  setHateCrossings?(value: boolean): void;
  hasValidRoute?(): boolean;
  hasCrossingObstacles?(): boolean;
}

interface AvoidRouter {
  processTransaction(): void;
  deleteConnector(connector: AvoidConnector): void;
  deleteShape(shape: AvoidShape): void;
  setRoutingParameter?(parameter: unknown, value: number): void;
  setRoutingOption?(option: unknown, value: boolean): void;
  delete?(): void;
}

interface AvoidShape {
  id?(): number;
}

interface AvoidApi {
  Router: new (flags: number) => AvoidRouter;
  RouterFlag: { OrthogonalRouting: { value: number } };
  Point: new (x: number, y: number) => AvoidPoint;
  Rectangle: new (centre: AvoidPoint, width: number, height: number) => object;
  ShapeRef: new (router: AvoidRouter, polygon: object) => AvoidShape;
  ConnEnd: new (point: AvoidPoint) => object;
  ConnRef: new (router: AvoidRouter, source: object, target: object) => AvoidConnector;
  RoutingParameter?: Record<string, { value: number }>;
  RoutingOption?: Record<string, { value: number }>;
}

interface AvoidLibraryModule {
  AvoidLib: {
    load(path?: string): Promise<void>;
    getInstance(): AvoidApi;
  };
}

export interface RoutingOptions {
  /**
   * How far a router terminal may drift from its assigned dock, in pixels, before
   * the route is rejected rather than corrected.
   *
   * Connectors use free endpoints, so libavoid moves them, and measurement shows it
   * preserves the along-side coordinate but shifts the terminal in the normal
   * direction (2 px and 6 px observed). The renderer draws the heel itself as
   * `border + outward x markerExtent`, so a normal-direction shift cannot be
   * persisted — accepting it would draw a short diagonal stub. Within this bound the
   * terminal is corrected onto the dock; beyond it the router docked somewhere else
   * (a different side is off by the card's width or height) and the route fails.
   */
  endpointDriftLimitPx: number;
  shapeBufferDistance: number;
  idealNudgingDistance: number;
  segmentPenalty: number;
  anglePenalty: number;
  crossingPenalty: number;
  sharedPathPenalty: number;
}

interface EndpointAssignment {
  side: AnchorSide;
  /** Persisted base ratio (parallel offset not baked in). */
  baseRatio: number;
  /** Effective ratio after the renderer's parallel offset. */
  ratio: number;
  /** Marker heel, which is where the route must start/end. */
  point: Point;
}

interface EdgeAssignment {
  source: EndpointAssignment;
  target: EndpointAssignment;
}

function validSide(value: AnchorSide | undefined): value is AnchorSide {
  return value === "left" || value === "right" || value === "top" || value === "bottom";
}

function orientationFor(
  edge: LayoutEdgeSnapshot,
  sourceRect: ReturnType<typeof rectForNode>,
  targetRect: ReturnType<typeof rectForNode>,
): { sourceSide: AnchorSide; targetSide: AnchorSide } {
  // Auto-generated docking is not a constraint: re-derive from the current
  // centres so an explicit Arrange can change direction. Saved sides still
  // guide locked/straight/manual routes below (F03).
  if (honoursSavedDocking(edge)) {
    const sourceSide = validSide(edge.sourceSide) ? edge.sourceSide : sideFromCenters(sourceRect, targetRect);
    const targetSide = validSide(edge.targetSide) ? edge.targetSide : sideFromCenters(targetRect, sourceRect);
    return { sourceSide, targetSide };
  }
  return {
    sourceSide: sideFromCenters(sourceRect, targetRect),
    targetSide: sideFromCenters(targetRect, sourceRect),
  };
}

function assignmentForEndpoint(
  edge: LayoutEdgeSnapshot,
  endpoint: "source" | "target",
  rect: ReturnType<typeof rectForNode>,
  otherRect: ReturnType<typeof rectForNode>,
  side: AnchorSide,
  allocatedRatio: number,
): EndpointAssignment {
  const docking = resolveDocking({
    rect,
    side,
    baseRatio: allocatedRatio,
    toward: { x: otherRect.x + otherRect.width / 2, y: otherRect.y + otherRect.height / 2 },
    offsetIndex: edge.offsetIndex ?? 0,
    totalEdges: edge.totalEdges ?? 1,
    markerExtent: (endpoint === "source" ? edge.sourceMarkerExtent : edge.targetMarkerExtent) ?? 0,
  });
  return { side: docking.side, baseRatio: docking.baseRatio, ratio: docking.ratio, point: docking.heel };
}

/**
 * Smallest on-screen distance between two connectors docking on the same side.
 *
 * Matches the parallel-edge spacing the renderer uses, so a card's attachments
 * read as one evenly-lit row rather than two competing rhythms.
 */
const MIN_ANCHOR_GAP_PX = 15;

interface GroupMember {
  edgeId: string;
  endpoint: "source" | "target";
  /** Ratio the connector would like, from the opposite card's centre. */
  ideal: number;
  /** Deterministic ordering coordinate along the side. */
  sortCoord: number;
  /** Manual docking the user already persisted, if any. */
  saved?: number;
  /** Length of the card side these members share, in pixels. */
  span: number;
}

function centreOf(rect: ReturnType<typeof rectForNode>): Point {
  return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
}

/**
 * Place connectors along one side of a card.
 *
 * They arrive sorted by where the card at the far end sits, which is the order
 * that avoids crossings. What was missing was WHERE to put them: the previous
 * rule discarded each connector's ideal position and spread the whole group
 * evenly across the entire side.
 *
 * On an ordinary card that looks tidy. On a fact table with fifty columns — two
 * thousand pixels tall — it drags every attachment across the full height even
 * when all the dimensions are clustered near the top, so each line sets off on
 * a long diagonal and they cross each other on the way. Even spacing is not the
 * goal; not colliding is.
 *
 * So each connector keeps its ideal position, and neighbours are pushed apart
 * only as far as the minimum gap requires. Forward pass enforces the gap
 * upward, backward pass pulls back anything driven past the end, and the order
 * is preserved throughout — which is what keeps the lines from crossing.
 */
function placeAlongSide(ideals: number[], span: number): number[] {
  const count = ideals.length;
  if (count === 0) return [];
  if (count === 1) return [clampAnchorRatio(ideals[0])];

  // A gap in ratio units. Connectors closer than this on screen overlap their
  // markers; expressing it in pixels keeps the rule the same on any card.
  const gap = span > 0 ? Math.min(MIN_ANCHOR_GAP_PX / span, (ANCHOR_RATIO_MAX - ANCHOR_RATIO_MIN) / (count - 1)) : 0;

  const placed = ideals.map((value) => clampAnchorRatio(value));
  for (let i = 1; i < count; i++) {
    if (placed[i] < placed[i - 1] + gap) placed[i] = placed[i - 1] + gap;
  }
  for (let i = count - 1; i >= 0; i--) {
    if (placed[i] > ANCHOR_RATIO_MAX) placed[i] = ANCHOR_RATIO_MAX;
    if (i > 0 && placed[i - 1] > placed[i] - gap) placed[i - 1] = placed[i] - gap;
  }
  for (let i = 0; i < count; i++) {
    if (placed[i] < ANCHOR_RATIO_MIN) placed[i] = ANCHOR_RATIO_MIN;
    if (i > 0 && placed[i] < placed[i - 1] + gap) placed[i] = Math.min(ANCHOR_RATIO_MAX, placed[i - 1] + gap);
  }
  return placed;
}

function addMember(groups: Map<string, GroupMember[]>, key: string, member: GroupMember): void {
  const list = groups.get(key);
  if (list) list.push(member);
  else groups.set(key, [member]);
}

/**
 * Assign one docking ratio per relationship endpoint.
 *
 * Connectors that share a card side must not all land on the same point: an
 * auto ratio taken straight from "point at the other card" clamps to an extreme
 * for every connector on an outward-facing side, which both makes parallel
 * joins indistinguishable and leaves libavoid no headroom — its endpoint
 * nudging then pushes the terminal past the ratio the renderer can reproduce.
 *
 * So each `(card, side)` group is spread across the usable anchor band, in the
 * same deterministic order the canvas has always used (offset along the side,
 * then relationship id). A single connector keeps its aimed ratio, and an
 * explicitly persisted manual ratio is never redistributed.
 */
function allocateAssignments(
  nodes: LayoutNodeSnapshot[],
  edges: LayoutEdgeSnapshot[],
  positions: Record<string, Point>,
): Map<string, EdgeAssignment> {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const groups = new Map<string, GroupMember[]>();
  const orientation = new Map<string, { sourceSide: AnchorSide; targetSide: AnchorSide }>();

  for (const edge of edges) {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) continue;
    const sourceRect = rectForNode(source, positions[source.id]);
    const targetRect = rectForNode(target, positions[target.id]);
    const { sourceSide, targetSide } = orientationFor(edge, sourceRect, targetRect);
    orientation.set(edge.id, { sourceSide, targetSide });

    const sourceCentre = centreOf(sourceRect);
    const targetCentre = centreOf(targetRect);
    const keepSaved = honoursSavedDocking(edge);
    const savedSource = keepSaved ? edge.sourceRatio : undefined;
    const savedTarget = keepSaved ? edge.targetRatio : undefined;
    // Auto docking aligns the two anchors to the overlap of the two cards'
    // ranges along the connecting side, which draws a straight connector when
    // the cards face each other squarely.
    //
    // When they do not overlap there is no such band, and the fallback is the
    // point where the line between the two card centres crosses this card's
    // border: the connector meets the card on the edge the two cards actually
    // face each other across, at the height the line between them is already
    // travelling at. Never the far side of a card, never the far end of an edge.
    const aligned = alignedSideRatios(sourceRect, sourceSide, targetRect, targetSide);
    addMember(groups, `${edge.source}:${sourceSide}`, {
      edgeId: edge.id,
      endpoint: "source",
      ideal: aligned.source,
      sortCoord: sourceSide === "left" || sourceSide === "right" ? targetCentre.y : targetCentre.x,
      saved: typeof savedSource === "number" && Number.isFinite(savedSource) ? savedSource : undefined,
      span: sourceSide === "left" || sourceSide === "right" ? sourceRect.height : sourceRect.width,
    });
    addMember(groups, `${edge.target}:${targetSide}`, {
      edgeId: edge.id,
      endpoint: "target",
      ideal: aligned.target,
      sortCoord: targetSide === "left" || targetSide === "right" ? sourceCentre.y : sourceCentre.x,
      saved: typeof savedTarget === "number" && Number.isFinite(savedTarget) ? savedTarget : undefined,
      span: targetSide === "left" || targetSide === "right" ? targetRect.height : targetRect.width,
    });
  }

  const ratioByEndpoint = new Map<string, number>();
  for (const members of groups.values()) {
    // Ordered by where the card at the far end sits: connectors that keep this
    // order along the side do not cross each other.
    members.sort((a, b) => a.sortCoord - b.sortCoord || a.edgeId.localeCompare(b.edgeId));
    const placed = placeAlongSide(members.map((member) => member.ideal), members[0]?.span ?? 0);
    members.forEach((member, index) => {
      const key = `${member.edgeId}:${member.endpoint}`;
      // A docking the user placed by hand is theirs, and is not re-spaced.
      ratioByEndpoint.set(
        key,
        member.saved !== undefined ? clampAnchorRatio(member.saved) : placed[index],
      );
    });
  }

  const assignment = new Map<string, EdgeAssignment>();
  for (const edge of edges) {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    const sides = orientation.get(edge.id);
    if (!source || !target || !sides) continue;
    const sourceRect = rectForNode(source, positions[source.id]);
    const targetRect = rectForNode(target, positions[target.id]);
    assignment.set(edge.id, {
      source: assignmentForEndpoint(
        edge, "source", sourceRect, targetRect, sides.sourceSide,
        ratioByEndpoint.get(`${edge.id}:source`) ?? 0.5,
      ),
      target: assignmentForEndpoint(
        edge, "target", targetRect, sourceRect, sides.targetSide,
        ratioByEndpoint.get(`${edge.id}:target`) ?? 0.5,
      ),
    });
  }
  return assignment;
}

function pointsFromPolyline(route: AvoidPolyline): Point[] {
  const points: Point[] = [];
  for (let index = 0; index < route.size(); index++) {
    const point = route.at(index);
    points.push({ x: point.x, y: point.y });
  }
  // `compactPoints` fails closed on a non-finite coordinate.
  return compactPoints(points);
}

/**
 * A relationship whose route the worker must not recompute: it is either
 * explicitly locked by the user or drawn in free-angle `straight` mode. Both
 * keep their stored geometry; only a lock also freezes the endpoints.
 *
 * A `straight` relationship keeps only *manual* free-angle bends: engine
 * generated orthogonal bends stored under `routeMode: "auto"` are not valid
 * straight geometry, so they are dropped and the route becomes the direct
 * heel-to-heel line (the renderer does the same).
 */
function retainedRoute(edge: LayoutEdgeSnapshot, assignment: EdgeAssignment): LayoutRoute {
  const waypoints = edge.pathMode === "straight" && edge.routeMode !== "manual" ? [] : edge.waypoints;
  const points = compactPoints([assignment.source.point, ...waypoints, assignment.target.point]);
  if (points.length < 2) throw noRoute(`relationship has no usable route: ${edge.id}`);
  return {
    edgeId: edge.id,
    points,
    waypoints: points.slice(1, -1),
    sourceSide: assignment.source.side,
    targetSide: assignment.target.side,
    sourceRatio: assignment.source.baseRatio,
    targetRatio: assignment.target.baseRatio,
    pathMode: edge.pathMode,
    routeMode: edge.routeMode,
    locked: edge.locked,
  };
}

/**
 * Whether a manual orthogonal route is still valid after a card move/resize.
 *
 * Stored manual bends are absolute, so when an endpoint card moves the segment
 * from the new heel to the first stored bend becomes diagonal, and when another
 * card moves onto the path the route now crosses a table. Spec §3: "Unlocked
 * manual routes remain unchanged while valid. Movement/resize repairs unlocked
 * routes whose endpoints changed or whose path now intersects a moved table."
 * A manual route that fails this check is repaired (re-routed) instead of being
 * retained, so it can never persist a diagonal, a through-table path, or a
 * terminal that points back into its endpoint card.
 *
 * The terminal exit check mirrors `validateRoutes`: the segment leaving the
 * source and the segment arriving at the target must both point away from the
 * card. A stored bend can land in the narrow gap between the heel and the card
 * border (still orthogonal, still outside the card interior), which makes the
 * retained route enter the card backward — the exact "relationship enters
 * target card" failure. It is detected here so the route is repaired instead.
 */
function manualRouteStillValid(
  edge: LayoutEdgeSnapshot,
  assignment: EdgeAssignment | undefined,
  nodes: LayoutNodeSnapshot[],
  positions: Record<string, Point>,
): boolean {
  if (!assignment) return false;
  if (edge.pathMode !== "orthogonal") return true;
  const points = compactPoints([assignment.source.point, ...edge.waypoints, assignment.target.point]);
  if (points.length < 2) return false;
  if (routeHasDiagonal(points)) return false;
  if (routeTouchesUnrelatedNode(points, edge, nodes, positions)) return false;
  // Terminals must exit away from the card (same contract as validateRoutes).
  const sourceOut = outwardVector(assignment.source.side);
  const targetOut = outwardVector(assignment.target.side);
  const first = points[1];
  const last = points[points.length - 2];
  const sourceHeel = points[0];
  const targetHeel = points[points.length - 1];
  if ((first.x - sourceHeel.x) * sourceOut.x + (first.y - sourceHeel.y) * sourceOut.y < -0.5) return false;
  if ((last.x - targetHeel.x) * targetOut.x + (last.y - targetHeel.y) * targetOut.y < -0.5) return false;
  return true;
}

function applyParameters(api: AvoidApi, router: AvoidRouter, options: RoutingOptions): void {
  const parameters = api.RoutingParameter;
  const setParameter = parameters ? router.setRoutingParameter?.bind(router) : undefined;
  if (parameters && setParameter) {
    const set = (name: string, value: number) => {
      // embind's enum `toWireType` reads `.value` off its argument, so the enum
      // *member object* must be passed whole — a plain number coerces to enum 0
      // (`segmentPenalty`) and every parameter silently lands on the wrong
      // option (verified against libavoid-js@0.5.0-beta.5).
      const entry = parameters[name];
      if (!entry) throw engineUnavailable(`libavoid routing parameter unavailable: ${name}`);
      setParameter(entry, value);
    };
    set("shapeBufferDistance", options.shapeBufferDistance);
    set("idealNudgingDistance", options.idealNudgingDistance);
    set("segmentPenalty", options.segmentPenalty);
    set("anglePenalty", options.anglePenalty);
    set("crossingPenalty", options.crossingPenalty);
    set("fixedSharedPathPenalty", options.sharedPathPenalty);
  }
  // Routing *options* are intentionally left off. With the enum-coercion bug
  // fixed, `nudgeOrthogonalSegmentsConnectedToShapes` actually applies now and
  // makes libavoid route through tables (large model: crossings 16 -> failing
  // "crosses a table"). It was never genuinely active before (the old `.value`
  // form silently dropped it), so omitting it preserves the previous effective
  // behaviour while the option value is verified separately.
}

/**
 * Thin obstacle rectangles covering a retained (locked) route, so eligible
 * neighbours are routed around occupied geometry instead of across it. The
 * terminal stubs are skipped so a lock does not seal the card boundary for
 * relationships that legitimately dock on the same side.
 */
function lockedCorridorObstacles(
  routes: Record<string, LayoutRoute>,
  halfWidth: number,
): Array<{ x: number; y: number; width: number; height: number }> {
  const boxes: Array<{ x: number; y: number; width: number; height: number }> = [];
  for (const route of Object.values(routes)) {
    const points = route.points;
    // Segment `index` joins points[index] and points[index + 1]. Segment 0 and
    // segment `length - 2` are the terminal stubs, deliberately left free.
    for (let index = 1; index < points.length - 2; index++) {
      const a = points[index];
      const b = points[index + 1];
      if (Math.abs(b.x - a.x) < 1 && Math.abs(b.y - a.y) < 1) continue;
      boxes.push({
        x: Math.min(a.x, b.x) - halfWidth,
        y: Math.min(a.y, b.y) - halfWidth,
        width: Math.abs(b.x - a.x) + halfWidth * 2,
        height: Math.abs(b.y - a.y) + halfWidth * 2,
      });
    }
  }
  return boxes;
}

/**
 * Route the complete edge batch with one libavoid router. The adapter stays
 * deliberately narrow because libavoid-js publishes a declaration path absent
 * from its package exports; no native object escapes this function.
 */
export async function routeEdges(
  nodes: LayoutNodeSnapshot[],
  edges: LayoutEdgeSnapshot[],
  positions: Record<string, Point>,
  options: RoutingOptions,
  /**
   * Whether this operation owns the node coordinates. `reroute-links` does not,
   * so a pre-existing overlap in a hand-arranged model must not make it fail:
   * it is forbidden from moving the cards anyway (F06).
   */
  requireNoOverlap = true,
  /**
   * `repair-routes` keeps user-authored manual geometry: only engine-generated
   * `auto` orthogonal routes are recomputed after a card move/resize, while
   * manual and locked routes stay (F05).
   */
  retainManual = false,
): Promise<Record<string, LayoutRoute>> {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const assignments = allocateAssignments(nodes, edges, positions);
  const routes: Record<string, LayoutRoute> = {};

  const retained = edges.filter(
    (edge) =>
      edge.locked ||
      edge.pathMode === "straight" ||
      (retainManual && edge.routeMode === "manual" && manualRouteStillValid(edge, assignments.get(edge.id), nodes, positions)),
  );
  const retainedIds = new Set(retained.map((edge) => edge.id));
  for (const edge of retained) {
    const assignment = assignments.get(edge.id);
    if (!assignment) throw geometryInvalid(`relationship endpoint disappeared: ${edge.id}`);
    routes[edge.id] = retainedRoute(edge, assignment);
  }

  const orthogonalEdges = edges.filter((edge) => edge.pathMode === "orthogonal" && !retainedIds.has(edge.id));
  if (!orthogonalEdges.length) {
    // `false`: a route crossing an unrelated card is reported as a quality
    // metric, not as a reason to produce no diagram at all. Arrange to the best
    // reasonable effort — a dense model still gets a layout, and the crossings
    // are counted for the caller to surface. Every other check stays
    // fail-closed.
    validateRoutes(nodes, edges, positions, routes, 0, requireNoOverlap, false);
    return routes;
  }

  let module: AvoidLibraryModule;
  try {
    module = await import("libavoid-js") as unknown as AvoidLibraryModule;
    await module.AvoidLib.load();
  } catch (error) {
    throw engineUnavailable(`libavoid engine unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
  const api = module.AvoidLib.getInstance();
  const router = new api.Router(api.RouterFlag.OrthogonalRouting.value);
  const shapeRefs: AvoidShape[] = [];
  const connectorRefs: AvoidConnector[] = [];
  try {
    applyParameters(api, router, options);
    for (const node of nodes) {
      const rect = rectForNode(node, positions[node.id]);
      const polygon = new api.Rectangle(new api.Point(rect.x + rect.width / 2, rect.y + rect.height / 2), rect.width, rect.height);
      shapeRefs.push(new api.ShapeRef(router, polygon));
    }
    // Occupied geometry of retained routes is an obstacle for eligible ones.
    for (const box of lockedCorridorObstacles(routes, options.shapeBufferDistance / 2)) {
      const polygon = new api.Rectangle(
        new api.Point(box.x + box.width / 2, box.y + box.height / 2),
        box.width,
        box.height,
      );
      shapeRefs.push(new api.ShapeRef(router, polygon));
    }
    for (const edge of orthogonalEdges) {
      const assignment = assignments.get(edge.id);
      if (!assignment) throw geometryInvalid(`relationship endpoint disappeared: ${edge.id}`);
      const source = new api.ConnEnd(new api.Point(assignment.source.point.x, assignment.source.point.y));
      const target = new api.ConnEnd(new api.Point(assignment.target.point.x, assignment.target.point.y));
      const connector = new api.ConnRef(router, source, target);
      connector.setHateCrossings?.(true);
      connectorRefs.push(connector);
    }
    router.processTransaction();
    for (let index = 0; index < orthogonalEdges.length; index++) {
      const edge = orthogonalEdges[index];
      const connector = connectorRefs[index];
      if (connector.hasValidRoute && !connector.hasValidRoute()) {
        throw noRoute(`libavoid returned no route for relationship: ${edge.id}`);
      }
      if (connector.hasCrossingObstacles?.()) {
        throw geometryInvalid(`libavoid routed through a table for relationship: ${edge.id}`);
      }
      const assignment = assignments.get(edge.id);
      if (!assignment) throw geometryInvalid(`relationship endpoint disappeared: ${edge.id}`);
      // libavoid emits the polyline between the two pinned endpoints, but it is
      // allowed to nudge a shared docking point along the card side. Adopt the
      // engine's actual terminals and persist the ratio they imply, so the
      // renderer draws exactly the polyline that was routed.
      const interior = pointsFromPolyline(connector.displayRoute());
      if (interior.length < 2) throw noRoute(`libavoid returned an empty route for relationship: ${edge.id}`);
      const sourceNode = nodeById.get(edge.source);
      const targetNode = nodeById.get(edge.target);
      if (!sourceNode || !targetNode) throw geometryInvalid(`relationship endpoint disappeared: ${edge.id}`);
      const sourceDock = adoptRoutedEndpoint({
        point: interior[0],
        rect: rectForNode(sourceNode, positions[sourceNode.id]),
        side: assignment.source.side,
        extent: edge.sourceMarkerExtent ?? 0,
        driftLimit: options.endpointDriftLimitPx,
        label: edge.id,
        assigned: assignment.source.point,
        offsetIndex: edge.offsetIndex ?? 0,
        totalEdges: edge.totalEdges ?? 1,
      });
      const targetDock = adoptRoutedEndpoint({
        point: interior[interior.length - 1],
        rect: rectForNode(targetNode, positions[targetNode.id]),
        side: assignment.target.side,
        extent: edge.targetMarkerExtent ?? 0,
        driftLimit: options.endpointDriftLimitPx,
        label: edge.id,
        assigned: assignment.target.point,
        offsetIndex: edge.offsetIndex ?? 0,
        totalEdges: edge.totalEdges ?? 1,
      });
      // A corrected terminal needs a stub that reaches the dock the renderer draws.
      // The stub is the heel projected onto the router's own first bend, so the stub
      // runs along the normal and the following segment along the side: both stay
      // axis-aligned, and no vertex is placed inside a card. Using the router's own
      // terminal instead would put a vertex inside the card whenever libavoid nudged
      // it inward, which the geometry validator rejects.
      const sourceVertical = assignment.source.side === "left" || assignment.source.side === "right";
      const targetVertical = assignment.target.side === "left" || assignment.target.side === "right";
      const sourceStub =
        sourceDock.corrected && interior.length >= 2
          ? (sourceVertical
              ? { x: sourceDock.heel.x, y: interior[1].y }
              : { x: interior[1].x, y: sourceDock.heel.y })
          : null;
      const targetStub =
        targetDock.corrected && interior.length >= 2
          ? (targetVertical
              ? { x: targetDock.heel.x, y: interior[interior.length - 2].y }
              : { x: interior[interior.length - 2].x, y: targetDock.heel.y })
          : null;
      const points = compactPoints([
        sourceDock.heel,
        ...(sourceStub ? [sourceStub] : []),
        ...interior.slice(1, -1),
        ...(targetStub ? [targetStub] : []),
        targetDock.heel,
      ]);
      if (points.length < 2) throw noRoute(`libavoid returned an empty route for relationship: ${edge.id}`);
      routes[edge.id] = {
        edgeId: edge.id,
        points,
        waypoints: points.slice(1, -1),
        sourceSide: assignment.source.side,
        targetSide: assignment.target.side,
        sourceRatio: sourceDock.baseRatio,
        targetRatio: targetDock.baseRatio,
        pathMode: edge.pathMode,
        routeMode: "auto",
        locked: false,
      };
    }
    validateRoutes(nodes, edges, positions, routes, 0, requireNoOverlap, false);
    return routes;
  } finally {
    for (const connector of connectorRefs) {
      try { router.deleteConnector(connector); } catch { /* binding owns connector */ }
    }
    for (const shape of shapeRefs) {
      try { router.deleteShape(shape); } catch { /* binding owns shape */ }
    }
    try { router.delete?.(); } catch { /* binding cleanup is best effort */ }
  }
}

/** Exposed for the geometry tests: the parallel offset the renderer applies. */
export { oppositeSide, parallelOffsetFor, ratioWithParallelOffset };
