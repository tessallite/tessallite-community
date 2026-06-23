/**
 * PipelineDiagram — ReactFlow visual of the query routing pipeline.
 * Renders a horizontal chain of stages (parser → binder → router → rewriter
 * → executor) plus optional aggregate-used and target-system nodes. Each
 * node carries a tooltip with the stage description and key/value details.
 */
import { useMemo } from "react";
import { useT } from "../../i18n";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import { Box, Tooltip, Typography } from "@mui/material";
import CodeIcon from "@mui/icons-material/Code";
import LinkIcon from "@mui/icons-material/Link";
import AltRouteIcon from "@mui/icons-material/AltRoute";
import EditNoteIcon from "@mui/icons-material/EditNote";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StorageIcon from "@mui/icons-material/Storage";
import LayersIcon from "@mui/icons-material/Layers";
import type { PipelineTrace, TraceStep } from "../../api/types";

type StageId = TraceStep["stage"] | "aggregate" | "pocket" | "target";

interface NodeData {
  id: StageId;
  title: string;
  detail: string;
  status: "ok" | "warn" | "error";
  meta: Record<string, string>;
  dimmed?: boolean;
}

const STAGE_PALETTE: Record<StageId, { bg: string; border: string; text: string }> = {
  parser: { bg: "#e3f2fd", border: "#1976d2", text: "#0d47a1" },
  binder: { bg: "#e8f5e9", border: "#2e7d32", text: "#1b5e20" },
  router: { bg: "#fff8e1", border: "#f9a825", text: "#795548" },
  rewriter: { bg: "#f3e5f5", border: "#7c4dff", text: "#4527a0" },
  executor: { bg: "#e0f7fa", border: "#00838f", text: "#006064" },
  aggregate: { bg: "#f3e5f5", border: "#7c4dff", text: "#4527a0" },
  pocket: { bg: "#ede7f6", border: "#5e35b1", text: "#311b92" },
  target: { bg: "#fff3e0", border: "#ef6c00", text: "#bf360c" },
};

function StageIcon({ stage }: { stage: StageId }) {
  const sx = { fontSize: 16, mr: 0.5 };
  switch (stage) {
    case "parser":
      return <CodeIcon sx={sx} />;
    case "binder":
      return <LinkIcon sx={sx} />;
    case "router":
      return <AltRouteIcon sx={sx} />;
    case "rewriter":
      return <EditNoteIcon sx={sx} />;
    case "executor":
      return <PlayArrowIcon sx={sx} />;
    case "aggregate":
      return <LayersIcon sx={sx} />;
    case "pocket":
      return <LayersIcon sx={sx} />;
    case "target":
    default:
      return <StorageIcon sx={sx} />;
  }
}

function StageNode({ data }: NodeProps<NodeData>) {
  const t = useT();
  const palette = STAGE_PALETTE[data.id];
  const dimmed = data.dimmed === true;
  return (
    <Tooltip
      arrow
      placement="top"
      title={
        <Box sx={{ p: 0.5, maxWidth: 320 }}>
          <Typography variant="caption" fontWeight={700} display="block">
            {data.title}
          </Typography>
          <Typography variant="caption" display="block" sx={{ opacity: 0.85, mb: 0.5 }}>
            {data.detail}
          </Typography>
          {dimmed && (
            <Typography variant="caption" display="block" sx={{ fontStyle: "italic", mb: 0.5 }}>
              {t("pipeline.notUsed")}
            </Typography>
          )}
          {Object.entries(data.meta).map(([k, v]) => (
            <Typography key={k} variant="caption" display="block">
              <strong>{metaLabel(t, k)}:</strong> {v}
            </Typography>
          ))}
        </Box>
      }
    >
      <Box
        sx={{
          width: 170,
          minHeight: 56,
          bgcolor: dimmed ? "#fafafa" : palette.bg,
          border: `1px dashed ${dimmed ? "#bdbdbd" : palette.border}`,
          borderStyle: dimmed ? "dashed" : "solid",
          borderLeft:
            data.status === "error"
              ? "4px solid #c62828"
              : data.status === "warn"
                ? "4px solid #ef6c00"
                : `4px solid ${dimmed ? "#bdbdbd" : palette.border}`,
          borderRadius: 1,
          px: 1,
          py: 0.75,
          color: dimmed ? "#9e9e9e" : palette.text,
          opacity: dimmed ? 0.55 : 1,
          filter: dimmed ? "grayscale(1)" : "none",
          // Read-only diagnostic diagram: nodes are not draggable, so do not
          // advertise a grab affordance (F-026-20).
          cursor: "default",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          transition: "opacity 120ms ease",
        }}
      >
        <Handle type="target" position={Position.Left} style={{ background: palette.border }} />
        <Handle type="source" position={Position.Right} style={{ background: palette.border }} />
        <Box display="flex" alignItems="center">
          <StageIcon stage={data.id} />
          <Typography
            component="span"
            sx={{ fontSize: 12, fontWeight: 700, lineHeight: 1.2 }}
            noWrap
          >
            {data.title}
          </Typography>
        </Box>
        <Typography
          variant="caption"
          sx={{
            fontSize: 10,
            color: palette.text,
            opacity: 0.85,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {data.detail}
        </Typography>
      </Box>
    </Tooltip>
  );
}

const NODE_TYPES = { stage: StageNode };

interface Props {
  trace: PipelineTrace;
}

// Pipeline trace `meta` keys are an OPEN domain — the backend can attach any
// snake_case key (protocol, route_type, from_tables, fingerprint, …). Known
// keys have curated labels in en.json (`pipeline.meta.<key>`); for anything
// unknown we humanise the raw key (route_type -> "Route type") instead of
// leaking "pipeline.meta.route_type" to the user (F-026-04). `t` returns the
// raw key string when a key is missing (see useT), which is how we detect a
// miss without a separate has-key API.
function humaniseMetaKey(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function metaLabel(t: (key: string) => string, rawKey: string): string {
  const i18nKey = `pipeline.meta.${rawKey.toLowerCase()}`;
  const resolved = t(i18nKey);
  return resolved === i18nKey ? humaniseMetaKey(rawKey) : resolved;
}

function buildMeta(step: TraceStep): Record<string, string> {
  const meta: Record<string, string> = {};
  for (const [k, v] of Object.entries(step.data ?? {})) {
    if (v === null || v === undefined) continue;
    if (k === "rewritten_sql") continue; // shown separately in QueryPanel
    if (Array.isArray(v)) {
      if (v.length === 0) continue;
      meta[k] = v.join(", ");
    } else if (typeof v === "object") {
      meta[k] = JSON.stringify(v);
    } else {
      meta[k] = String(v);
    }
  }
  return meta;
}

export default function PipelineDiagram({ trace }: Props) {
  const t = useT();

  const layout = useMemo(() => {
    const colWidth = 200;
    const rfNodes: Node[] = [];
    const rfEdges: Edge[] = [];

    // When the router routed to an aggregate, the source-execution path
    // isn't used: the executor never touches the source tables and the
    // source system is bypassed entirely. Dim both nodes and the edge
    // between them. The aggregate node renders alongside, highlighted.
    const usedCachedPath = !!trace.aggregate_used || !!trace.pocket_used;
    const targetDimmed = usedCachedPath;
    const executorDimmed = usedCachedPath;

    trace.steps.forEach((step, i) => {
      const stageDimmed = step.stage === "executor" && executorDimmed;
      const stageDetail =
        step.stage === "executor" && executorDimmed
          ? t("pipeline.executorBypassedDetail")
          : step.detail;
      const stageTitle =
        step.stage === "executor" && executorDimmed
          ? t("pipeline.executorBypassed")
          : step.title;
      rfNodes.push({
        id: step.stage,
        type: "stage",
        position: { x: i * colWidth, y: 0 },
        data: {
          id: step.stage,
          title: stageTitle,
          detail: stageDetail,
          status: step.status,
          meta: buildMeta(step),
          dimmed: stageDimmed,
        } as NodeData,
      });
      if (i > 0) {
        const prev = trace.steps[i - 1].stage;
        const edgeDimmed = step.stage === "executor" && executorDimmed;
        rfEdges.push({
          id: `${prev}-${step.stage}`,
          source: prev,
          target: step.stage,
          type: "smoothstep",
          animated: !edgeDimmed,
          style: {
            stroke: edgeDimmed ? "#bdbdbd" : "#90a4ae",
            strokeDasharray: edgeDimmed ? "4 4" : undefined,
            opacity: edgeDimmed ? 0.55 : 1,
          },
        });
      }
    });

    // Aggregate side-node attached to the rewriter
    if (trace.aggregate_used) {
      const rewriterIndex = trace.steps.findIndex((s) => s.stage === "rewriter");
      const x = (rewriterIndex >= 0 ? rewriterIndex : trace.steps.length - 1) * colWidth;
      rfNodes.push({
        id: "aggregate",
        type: "stage",
        position: { x, y: 110 },
        data: {
          id: "aggregate",
          title: trace.aggregate_used.physical_table_name,
          detail: t("pipeline.preAggregateDetail"),
          status: "ok",
          meta: {
            Generator: trace.aggregate_used.creation_reason ?? "—",
            Status: trace.aggregate_used.status ?? "—",
            Grain: trace.aggregate_used.grain.join(", ") || "—",
          },
        } as NodeData,
      });
      if (rewriterIndex >= 0) {
        rfEdges.push({
          id: `rewriter-aggregate`,
          source: "rewriter",
          target: "aggregate",
          type: "smoothstep",
          animated: true,
          style: { stroke: "#7c4dff", strokeDasharray: "4 2" },
          label: t("pipeline.uses"),
          labelStyle: { fontSize: 10, fill: "#7c4dff" },
        });
      }
    }

    // Pocket side-node attached to the rewriter
    if (trace.pocket_used) {
      const rewriterIndex = trace.steps.findIndex((s) => s.stage === "rewriter");
      const x = (rewriterIndex >= 0 ? rewriterIndex : trace.steps.length - 1) * colWidth;
      rfNodes.push({
        id: "pocket",
        type: "stage",
        position: { x, y: 110 },
        data: {
          id: "pocket",
          title: trace.pocket_used.physical_table_name,
          detail: t("pipeline.pocketDetail"),
          status: "ok",
          meta: {
            Status: trace.pocket_used.status ?? "—",
            Policy: trace.pocket_used.refresh_policy ?? "—",
          },
        } as NodeData,
      });
      if (rewriterIndex >= 0) {
        rfEdges.push({
          id: `rewriter-pocket`,
          source: "rewriter",
          target: "pocket",
          type: "smoothstep",
          animated: true,
          style: { stroke: "#5e35b1", strokeDasharray: "4 2" },
          label: t("pipeline.uses"),
          labelStyle: { fontSize: 10, fill: "#5e35b1" },
        });
      }
    }

    // Target system node attached to the executor (or rewriter for dry-run)
    if (trace.target_system) {
      const lastStage = trace.steps[trace.steps.length - 1]?.stage ?? "rewriter";
      const x = trace.steps.length * colWidth;
      rfNodes.push({
        id: "target",
        type: "stage",
        position: { x, y: 0 },
        data: {
          id: "target",
          title: trace.target_system.name ?? t("pipeline.sourceSystemDefault"),
          detail: targetDimmed
            ? t("pipeline.sourceSystemBypassed")
            : t("pipeline.sourceSystemDirect"),
          status: "ok",
          meta: {
            Type: trace.target_system.type ?? "—",
            Location: trace.target_system.location ?? "—",
          },
          dimmed: targetDimmed,
        } as NodeData,
      });
      rfEdges.push({
        id: `${lastStage}-target`,
        source: lastStage,
        target: "target",
        type: "smoothstep",
        animated: !targetDimmed,
        style: {
          stroke: targetDimmed ? "#bdbdbd" : "#ef6c00",
          strokeDasharray: targetDimmed ? "4 4" : undefined,
          opacity: targetDimmed ? 0.55 : 1,
        },
        label: targetDimmed ? t("pipeline.bypassed") : t("pipeline.queries"),
        labelStyle: {
          fontSize: 10,
          fill: targetDimmed ? "#9e9e9e" : "#ef6c00",
        },
      });
    }

    return { rfNodes, rfEdges };
  }, [trace, t]);

  // Stable identity key: forces ReactFlow to fully remount when the pipeline
  // SHAPE changes, but NOT on every render. The layout memo depends on `t`,
  // which useT() returns as a fresh function each render, so `layout` is a new
  // object every render; a useNodesState/useEffect sync would therefore reset
  // the nodes on every render and make the diagram flash/disappear while
  // fitView (run once on mount) holds a stale viewport. Deriving the key from
  // the trace's stage signature keeps it stable across those churning renders.
  const traceKey = useMemo(() => {
    const stages = trace.steps.map((s) => s.stage).join("|");
    const agg = trace.aggregate_used ? "|agg" : "";
    const pkt = trace.pocket_used ? "|pkt" : "";
    return stages + agg + pkt;
  }, [trace]);

  return (
    <Box
      sx={{
        width: "100%",
        height: 280,
        border: "1px solid",
        borderColor: "divider",
        borderRadius: 1,
        bgcolor: "background.paper",
      }}
    >
      <ReactFlow
        key={traceKey}
        nodes={layout.rfNodes}
        edges={layout.rfEdges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        proOptions={{ hideAttribution: true }}
        zoomOnScroll
        zoomOnPinch
        panOnDrag
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        minZoom={0.2}
        maxZoom={2}
      >
        <Controls showInteractive={false} position="bottom-right" />
        <Background gap={16} color="#f0f0f0" />
      </ReactFlow>
    </Box>
  );
}
