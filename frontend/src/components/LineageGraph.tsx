/**
 * LineageGraph — ReactFlow canvas for model lineage.
 *
 * Node types:
 *   "source"    — raw data source (blue)
 *   "semantic"  — semantic model table (green)
 *   "aggregate" — pre-aggregate table (purple); badge by generator
 *   "target"    — destination connection (amber)
 *   "column"    — physical source column
 *   "field"     — semantic field exposed by the model
 *
 * Each node renders a MUI Tooltip with description + metadata table.
 * Layout: auto-positioned left-to-right via dagre.
 */
import { useEffect, useMemo } from "react";
import { useT } from "../i18n";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import dagre from "@dagrejs/dagre";
import { Box, Tooltip, Typography } from "@mui/material";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import CalculateIcon from "@mui/icons-material/Calculate";
import PersonIcon from "@mui/icons-material/Person";
import StorageIcon from "@mui/icons-material/Storage";
import OutputIcon from "@mui/icons-material/Output";
import HubIcon from "@mui/icons-material/Hub";
import LayersIcon from "@mui/icons-material/Layers";
import ViewColumnIcon from "@mui/icons-material/ViewColumn";
import type { LineageGraph as LineageData, LineageNode as LineageNodeData } from "../api/types";

const NODE_WIDTH = 220;
const NODE_HEIGHT = 64;

type LineageNodeType = "source" | "semantic" | "aggregate" | "target" | "column" | "field";

interface NodePalette {
  bg: string;
  border: string;
  text: string;
}

const NODE_PALETTE: Record<LineageNodeType, NodePalette> = {
  source: { bg: "#e3f2fd", border: "#1976d2", text: "#0d47a1" },
  semantic: { bg: "#e8f5e9", border: "#388e3c", text: "#1b5e20" },
  aggregate: { bg: "#f3e5f5", border: "#7c4dff", text: "#4527a0" },
  target: { bg: "#fff3e0", border: "#ef6c00", text: "#bf360c" },
  column: { bg: "#eceff1", border: "#546e7a", text: "#263238" },
  field: { bg: "#fce4ec", border: "#c2185b", text: "#880e4f" },
};

function formatDate(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

function NodeIcon({ type }: { type: LineageNodeType }) {
  const sx = { fontSize: 16, mr: 0.5 };
  if (type === "source") return <StorageIcon sx={sx} />;
  if (type === "semantic") return <HubIcon sx={sx} />;
  if (type === "aggregate") return <LayersIcon sx={sx} />;
  if (type === "column" || type === "field") return <ViewColumnIcon sx={sx} />;
  return <OutputIcon sx={sx} />;
}

function GeneratorBadge({ creationReason }: { creationReason?: string | null }) {
  const t = useT();
  if (!creationReason) return null;
  const sx = { fontSize: 14, ml: 0.5 };
  if (creationReason === "ai")
    return (
      <Tooltip title={t("lineage.generatedByAi")}>
        <AutoAwesomeIcon sx={{ ...sx, color: "#1976d2" }} />
      </Tooltip>
    );
  if (creationReason === "auto")
    return (
      <Tooltip title={t("lineage.generatedByAuto")}>
        <CalculateIcon sx={{ ...sx, color: "#6a1b9a" }} />
      </Tooltip>
    );
  if (creationReason === "manual")
    return (
      <Tooltip title={t("lineage.createdManually")}>
        <PersonIcon sx={{ ...sx, color: "#455a64" }} />
      </Tooltip>
    );
  return null;
}

function NodeTooltip({ node }: { node: LineageNodeData }) {
  const t = useT();
  function getGeneratorLabel(reason: string | null | undefined): string | null {
    if (!reason) return null;
    if (reason === "ai") return t("lineage.generatorAi");
    if (reason === "auto") return t("lineage.generatorAuto");
    if (reason === "manual") return t("lineage.generatorManual");
    return reason;
  }
  const generatorLabel = getGeneratorLabel(node.creation_reason);
  return (
    <Box sx={{ p: 0.5, maxWidth: 320 }}>
      <Typography variant="caption" fontWeight={700} display="block" sx={{ mb: 0.25 }}>
        {node.label}
      </Typography>
      {node.description && (
        <Typography variant="caption" color="inherit" display="block" sx={{ mb: 0.5, opacity: 0.85 }}>
          {node.description}
        </Typography>
      )}
      {node.type === "aggregate" && (
        <>
          {generatorLabel && (
            <Typography variant="caption" display="block">
              <strong>{t("lineage.generator")}:</strong> {generatorLabel}
            </Typography>
          )}
          {node.status && (
            <Typography variant="caption" display="block">
              <strong>{t("lineage.status")}:</strong> {node.status}
            </Typography>
          )}
          <Typography variant="caption" display="block">
            <strong>{t("lineage.lastRefresh")}:</strong> {formatDate(node.last_refreshed_at)}
          </Typography>
        </>
      )}
      {node.meta &&
        Object.entries(node.meta)
          .filter(([k]) =>
            // For aggregates, the dedicated fields above already cover these.
            node.type !== "aggregate" ||
            !["Status", "Generator", "Last refresh"].includes(k),
          )
          .map(([k, v]) => (
            <Typography key={k} variant="caption" display="block">
              <strong>{k}:</strong> {v}
            </Typography>
          ))}
    </Box>
  );
}

function LineageNodeRenderer({ data }: NodeProps<{ node: LineageNodeData; taggedColumnCount?: number }>) {
  const t = useT();
  const node = data.node;
  const taggedCount = data.taggedColumnCount ?? 0;
  const type = (node.type as LineageNodeType) ?? "source";
  const palette = NODE_PALETTE[type] ?? NODE_PALETTE.source;
  const lastRefreshShort = node.last_refreshed_at
    ? new Date(node.last_refreshed_at).toLocaleDateString()
    : null;

  return (
    <Tooltip title={<NodeTooltip node={node} />} arrow placement="top">
      <Box
        sx={{
          width: NODE_WIDTH,
          minHeight: NODE_HEIGHT,
          bgcolor: palette.bg,
          border: `1px solid ${palette.border}`,
          borderRadius: 1,
          px: 1,
          py: 0.5,
          color: palette.text,
          fontSize: 12,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          cursor: "default",
        }}
      >
        <Handle type="target" position={Position.Left} style={{ background: palette.border }} />
        <Handle type="source" position={Position.Right} style={{ background: palette.border }} />
        <Box display="flex" alignItems="center">
          <NodeIcon type={type} />
          <Typography
            component="span"
            sx={{ fontSize: 12, fontWeight: 600, flexGrow: 1, lineHeight: 1.2 }}
            noWrap
          >
            {node.label}
          </Typography>
          {type === "aggregate" && <GeneratorBadge creationReason={node.creation_reason} />}
        </Box>
        {type === "semantic" && (!!node.downstream_asset_count || !!taggedCount) && (
          <Box display="flex" gap={1} sx={{ fontSize: 10, mt: 0.25, opacity: 0.8 }}>
            {!!node.downstream_asset_count && (
              <Typography component="span" sx={{ fontSize: 10 }}>
                {node.downstream_asset_count} {node.downstream_asset_count !== 1 ? t("lineage.downstreamAssetsPlural") : t("lineage.downstreamAsset")}
              </Typography>
            )}
            {!!taggedCount && (
              <Typography component="span" sx={{ fontSize: 10 }}>
                {taggedCount} {taggedCount !== 1 ? t("lineage.taggedColsPlural") : t("lineage.taggedCol")}
              </Typography>
            )}
          </Box>
        )}
        {type === "aggregate" && (
          <Box display="flex" alignItems="center" mt={0.25} gap={0.5} sx={{ fontSize: 10 }}>
            {node.status && (
              <Box
                component="span"
                sx={{
                  px: 0.5,
                  borderRadius: 0.5,
                  bgcolor: node.status === "active" ? "#c8e6c9" : "#eeeeee",
                  color: node.status === "active" ? "#1b5e20" : "#616161",
                  fontSize: 10,
                  fontWeight: 600,
                }}
              >
                {node.status}
              </Box>
            )}
            {lastRefreshShort && (
              <Typography component="span" sx={{ fontSize: 10, opacity: 0.75 }}>
                {t("lineage.refreshed", { date: lastRefreshShort })}
              </Typography>
            )}
          </Box>
        )}
      </Box>
    </Tooltip>
  );
}

const NODE_TYPES = { lineage: LineageNodeRenderer };

function layoutGraph(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", ranksep: 90, nodesep: 36 });
  g.setDefaultEdgeLabel(() => ({}));

  nodes.forEach((n) => g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  dagre.layout(g);

  return nodes.map((n) => {
    const pos = g.node(n.id);
    return { ...n, position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 } };
  });
}

interface Props {
  data: LineageData;
  taggedColumnCounts?: Map<string, number>;
}

export default function LineageGraph({ data, taggedColumnCounts }: Props) {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  // Build a node-id set so we can drop edges that reference unknown nodes
  // (e.g. orphaned column-level lineage rows). Otherwise dagre will create
  // ghost nodes and ReactFlow will warn about missing sources/targets.
  const validNodeIds = useMemo(() => new Set(data.nodes.map((n) => n.id)), [data]);

  useEffect(() => {
    const rfNodes: Node[] = data.nodes.map((n) => ({
      id: n.id,
      data: { node: n, taggedColumnCount: taggedColumnCounts?.get(n.label) ?? 0 },
      type: "lineage",
      position: { x: 0, y: 0 },
    }));

    const rfEdges: Edge[] = data.edges
      .filter((e) => validNodeIds.has(e.source) && validNodeIds.has(e.target))
      .map((e, idx) => ({
        id: `${e.source}-${e.target}-${idx}`,
        source: e.source,
        target: e.target,
        label: e.label,
        type: "smoothstep",
        animated: true,
        style: { stroke: "#90a4ae" },
        labelStyle: { fontSize: 10, fill: "#546e7a" },
      }));

    const laid = layoutGraph(rfNodes, rfEdges);
    setNodes(laid);
    setEdges(rfEdges);
  }, [data, validNodeIds, taggedColumnCounts, setNodes, setEdges]);

  return (
    <div style={{ width: "100%", height: "520px", border: "1px solid #e0e0e0", borderRadius: 8 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
      >
        <Controls />
        <MiniMap
          nodeColor={(n) => {
            const node = (n.data as { node?: LineageNodeData })?.node;
            const t = (node?.type as LineageNodeType) ?? "source";
            return NODE_PALETTE[t]?.bg ?? "#eee";
          }}
        />
        <Background gap={16} color="#f0f0f0" />
      </ReactFlow>
    </div>
  );
}
