import type ELK from "elkjs/lib/elk-api.js";
import type {
  ElkEdgeSection,
  ElkNode,
  ElkPort,
} from "elkjs/lib/elk-api.js";
import config from "./config.json";
import type {
  AnchorSide,
  LayoutDirection,
  LayoutEdgeSnapshot,
  LayoutEngineOptions,
  LayoutEngineOutput,
  LayoutNodeSnapshot,
  LayoutPreset,
  LayoutSnapshot,
  LayoutSpacing,
  Point,
} from "./types";
import { honoursSavedDocking } from "./docking";

const elkLayoutOptions = {
  "elk.algorithm": "layered",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
  "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
  "elk.layered.nodePlacement.bk.fixedAlignment": "BALANCED",
  "elk.spacing.edgeNode": "24",
  "elk.spacing.edgeEdge": "20",
  "elk.port.borderOffset": "4",
  "elk.portConstraints": "FIXED_ORDER",
};

function directionName(direction: LayoutDirection): string {
  return direction === "RIGHT" ? "RIGHT" : "DOWN";
}

/**
 * Canvas sides use compass-neutral names (`left`/`right`/`top`/`bottom`); ELK's
 * port-side enum uses `WEST`/`EAST`/`NORTH`/`SOUTH`. An exhaustive mapping avoids
 * emitting `LEFT`/`RIGHT`/`TOP`/`BOTTOM`, which are not valid ELK enum values and
 * break the second Arrange of a saved or manually docked diagram (F02).
 */
const ANCHOR_SIDE_TO_ELK: Record<AnchorSide, string> = {
  left: "WEST",
  right: "EAST",
  top: "NORTH",
  bottom: "SOUTH",
};

function portSide(edge: LayoutEdgeSnapshot, endpoint: "source" | "target", direction: LayoutDirection): string {
  // A saved auto side is not a constraint: ELK re-derives the side from the
  // chosen direction, so a direction change re-orients the diagram. Saved sides
  // still bind locked/straight/manual routes (F03).
  const side = honoursSavedDocking(edge)
    ? (endpoint === "source" ? edge.sourceSide : edge.targetSide)
    : undefined;
  if (side) return ANCHOR_SIDE_TO_ELK[side];
  if (direction === "RIGHT") return endpoint === "source" ? "EAST" : "WEST";
  return endpoint === "source" ? "SOUTH" : "NORTH";
}

function portId(edgeId: string, endpoint: "source" | "target"): string {
  return `${edgeId}:${endpoint}`;
}

function nodeIdFromPort(id: string): string {
  return id.split(":")[0];
}

function measuredPort(id: string, side: string, index: number): ElkPort {
  return {
    id,
    width: 1,
    height: 1,
    layoutOptions: {
      "elk.port.side": side,
      "elk.port.index": String(index),
    },
  };
}

export function toElkGraph(
  snapshot: LayoutSnapshot,
  movableIds: Set<string>,
  options: LayoutEngineOptions,
): ElkNode {
  const nodes = snapshot.nodes.filter((node) => movableIds.has(node.id));
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = snapshot.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  return {
    id: "canvas",
    layoutOptions: {
      ...elkLayoutOptions,
      "elk.direction": directionName(options.direction),
      "elk.spacing.nodeNode": String(options.nodeSpacing),
      "elk.layered.spacing.nodeNodeBetweenLayers": String(options.layerSpacing),
      "elk.layered.randomizationSeed": "17",
    },
    children: nodes.map((node) => {
      // Deterministic port order: the order the relationships appear in the
      // snapshot, so parallel joins keep stable distinct docking points.
      let portIndex = 0;
      const ports: ElkPort[] = [];
      for (const edge of edges) {
        if (edge.source === node.id) ports.push(measuredPort(portId(edge.id, "source"), portSide(edge, "source", options.direction), portIndex++));
        if (edge.target === node.id) ports.push(measuredPort(portId(edge.id, "target"), portSide(edge, "target", options.direction), portIndex++));
      }
      return {
        id: node.id,
        x: 0,
        y: 0,
        width: node.width,
        height: node.height,
        ports,
        layoutOptions: {
          "elk.portConstraints": "FIXED_ORDER",
        },
      };
    }),
    edges: edges.map((edge) => ({
      id: edge.id,
      sources: [portId(edge.id, "source")],
      targets: [portId(edge.id, "target")],
    })),
  };
}

function translatePoint(point: { x?: number; y?: number }, parentX: number, parentY: number): Point {
  return { x: (point.x ?? 0) + parentX, y: (point.y ?? 0) + parentY };
}

function sectionsToPoints(section: ElkEdgeSection, parentX: number, parentY: number): Point[] {
  return [
    translatePoint(section.startPoint, parentX, parentY),
    ...(section.bendPoints ?? []).map((point) => translatePoint(point, parentX, parentY)),
    translatePoint(section.endPoint, parentX, parentY),
  ];
}

function collectEdgeRoutes(
  graph: ElkNode,
  parentX = 0,
  parentY = 0,
): Array<{ edgeId: string; points: Point[] }> {
  const x = parentX + (graph.x ?? 0);
  const y = parentY + (graph.y ?? 0);
  const routes: Array<{ edgeId: string; points: Point[] }> = [];
  for (const edge of graph.edges ?? []) {
    const section = edge.sections?.[0];
    if (section) routes.push({ edgeId: edge.id, points: sectionsToPoints(section, x, y) });
  }
  for (const child of graph.children ?? []) routes.push(...collectEdgeRoutes(child, x, y));
  return routes;
}

/**
 * One ELK engine per worker lifetime.
 *
 * The worker is the lifecycle unit (the client terminates it on cancel,
 * timeout, unmount and model switch), so the engine must not be torn down after
 * every operation: doing so paid ELK startup on each layout and called
 * `terminateWorker()` on a transport that is not always a real `Worker`.
 */
let elkEngine: InstanceType<typeof ELK> | null = null;

async function engine(): Promise<InstanceType<typeof ELK>> {
  if (!elkEngine) {
    if (typeof Worker !== "undefined") {
      // Browser worker: API-only entry driven by the real ELK worker file. The
      // bundled runtime must never be evaluated here: its GWT bootstrap detects the
      // worker's self and registers self.onmessage instead of exporting the
      // web-worker module, so the factory would construct undefined (the
      // "is not a constructor" failure, elkjs issue #141).
      const { default: ElkApi } = await import("elkjs/lib/elk-api.js");
      elkEngine = new ElkApi({
        workerUrl: new URL("elkjs/lib/elk-worker.min.js", import.meta.url).href,
      });
    } else if (import.meta.env.MODE === "test") {
      // Node test runs only: the bundled entry's in-process adapter. Imported
      // dynamically so the browser build never evaluates this runtime. `Worker`
      // being undefined is a capability check, not positive proof of Node — a
      // browser without usable worker support must fail clearly, not land here.
      const { default: ElkBundled } = await import("elkjs/lib/elk.bundled.js");
      elkEngine = new ElkBundled();
    } else {
      throw new Error(
        "ELK layout requires Web Worker support, which this environment does not provide",
      );
    }
  }
  return elkEngine;
}

export async function layoutWithElk(
  snapshot: LayoutSnapshot,
  movableIds: Set<string>,
  options: LayoutEngineOptions,
): Promise<LayoutEngineOutput> {
  if (movableIds.size === 0) return { positions: {}, routes: [] };
  const graph = toElkGraph(snapshot, movableIds, options);
  const elk = await engine();
  const laidOut = await elk.layout(graph, { measureExecutionTime: true });
  const positions: Record<string, Point> = {};
  for (const child of laidOut.children ?? []) {
    if (!child.id || !movableIds.has(child.id)) continue;
    if (!Number.isFinite(child.x) || !Number.isFinite(child.y)) throw new Error(`ELK returned an invalid position for ${child.id}`);
    positions[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
  }
  if (Object.keys(positions).length !== movableIds.size) throw new Error("ELK did not return every movable table");
  return { positions, routes: collectEdgeRoutes(laidOut) };
}

/** Spacing is owned by `config.json` so no preset value is duplicated here. */
export function spacingForPreset(preset: LayoutPreset, spacing: LayoutSpacing): { nodeSpacing: number; layerSpacing: number } {
  const presetConfig = config.presets[preset] ?? config.presets.hierarchical;
  const chosen = spacing === "dense" ? presetConfig.dense : presetConfig.normal;
  return { nodeSpacing: chosen.nodeSpacing, layerSpacing: chosen.layerSpacing };
}
