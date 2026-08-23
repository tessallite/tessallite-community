/**
 * ERDTableNode — custom ReactFlow node that renders a table box in ERD style.
 * Shows table name, type badge, and a scrollable attribute list.
 */
import { memo, useEffect, useState } from "react";
import { useT } from "../../i18n";
import {
  Handle,
  NodeResizer,
  Position,
  useStore,
  useUpdateNodeInternals,
} from "reactflow";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import EditIcon from "@mui/icons-material/Edit";
import TableRowsIcon from "@mui/icons-material/TableRows";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import BoltIcon from "@mui/icons-material/Bolt";
import ViewCompactIcon from "@mui/icons-material/ViewCompact";
import { modelTablesApi, tableAttributesApi } from "../../api/client";
import { useSources } from "../../api/hooks";
import type { ModelTable, TableAttribute } from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import { font, nodeHeader, nodeHeaderFallback } from "../../theme/tokens";
import TableEditDialog from "../Panels/TableEditDialog";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";

/**
 * Name sets (lower-cased) that drive fact-table column segmentation (A1).
 * Canvas.tsx computes one `SegmentationRefs` per fact table and attaches it
 * here; dim tables do not receive segmentation (they get the hierarchy
 * grouping overlay in A3 instead).
 */
export interface SegmentationRefs {
  measureCols: Set<string>;
  dimCols: Set<string>;
  keyCols: Set<string>;
  levelCols: Set<string>;
}

/**
 * One hierarchy-on-this-table descriptor used by A3 (hierarchy grouping
 * overlay on dim nodes).  One entry per hierarchy that contributes any
 * level whose key attribute lives on this table.
 */
export interface HierarchyGroupOnTable {
  name: string;
  levels: Array<{ name: string; ordinal: number; column_name: string }>;
}

/**
 * Counts for the aggregate + pocket overlay (A6).  Only populated for fact
 * tables.  Chips are hidden on the node when both counts are zero so the
 * header stays clean on new models.
 */
export interface OverlayCounts {
  aggregates: number;
  pockets: number;
}

export interface ERDNodeData {
  table: ModelTable;
  projectId: string;
  modelId: string;
  segmentation?: SegmentationRefs;
  hierarchyGroups?: HierarchyGroupOnTable[];
  overlay?: OverlayCounts;
  /**
   * Dimmed by the active persona overlay (8.B.7).  When true, the node
   * renders greyscale at reduced opacity and becomes visually non-interactive,
   * signalling that none of its objects are in the selected persona's
   * allow lists.
   */
  dimmed?: boolean;
  /**
   * Read-only canvas (a `?readonly=1` share link). When true the table-edit
   * affordances (rename / classify / open editor via header click) are hidden
   * so the view honours its "read only" promise (F-026-16). Server RBAC is the
   * real control; this stops the UI contradicting itself.
   */
  readOnly?: boolean;
}

type SegmentKey = "measures" | "dimensions" | "keys" | "levels" | "unused";

const SEGMENT_ORDER: Array<{ key: SegmentKey; label: string }> = [
  { key: "measures", label: "erdSegment.measures" },
  { key: "dimensions", label: "erdSegment.dimensions" },
  { key: "keys", label: "erdSegment.dimensionKeys" },
  { key: "levels", label: "erdSegment.levels" },
  { key: "unused", label: "erdSegment.unused" },
];

function sectionStorageKey(tableId: string): string {
  return `tsl.nodeSect.${tableId}`;
}

function loadSectionState(tableId: string): Record<SegmentKey, boolean> {
  const defaults: Record<SegmentKey, boolean> = {
    measures: true,
    dimensions: true,
    keys: true,
    levels: true,
    unused: true,
  };
  if (typeof window === "undefined") return defaults;
  try {
    const raw = window.sessionStorage.getItem(sectionStorageKey(tableId));
    if (!raw) return defaults;
    const parsed = JSON.parse(raw) as Partial<Record<SegmentKey, boolean>>;
    return {
      measures: parsed.measures ?? true,
      dimensions: parsed.dimensions ?? true,
      keys: parsed.keys ?? true,
      levels: parsed.levels ?? true,
      unused: parsed.unused ?? true,
    };
  } catch {
    return defaults;
  }
}

function saveSectionState(tableId: string, state: Record<SegmentKey, boolean>) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(sectionStorageKey(tableId), JSON.stringify(state));
  } catch {
    // sessionStorage full or disabled — silently ignore.
  }
}

function bucketAttribute(
  attrName: string,
  seg: SegmentationRefs,
): SegmentKey {
  const lc = attrName.toLowerCase();
  if (seg.measureCols.has(lc)) return "measures";
  if (seg.dimCols.has(lc)) return "dimensions";
  if (seg.keyCols.has(lc)) return "keys";
  if (seg.levelCols.has(lc)) return "levels";
  return "unused";
}

function typeInfo(dt: string): { label: string; color: string } {
  const t = dt.toLowerCase();
  if (/\bint|bigint|smallint|serial/.test(t)) return { label: "int", color: "#3A5EA8" };
  if (/float|double|decimal|numeric|real|money/.test(t)) return { label: "num", color: "#006C35" };
  if (/timestamp|datetime/.test(t)) return { label: "ts", color: "#A67C00" };
  if (/\bdate\b/.test(t)) return { label: "dt", color: "#A67C00" };
  if (/bool/.test(t)) return { label: "bool", color: "#6B4C8A" };
  if (/char|text|varchar|string|name/.test(t)) return { label: "str", color: "#5A6577" };
  if (/uuid/.test(t)) return { label: "uuid", color: "#5A6577" };
  if (/json|array/.test(t)) return { label: "json", color: "#6B4C3A" };
  const fallback = t.replace(/[^a-z0-9]/g, "").slice(0, 4);
  return { label: fallback || "col", color: "#5A6577" };
}

// Header styles come from central tokens — no hardcoded colours here.
const FALLBACK_HEADER = nodeHeaderFallback;

// Tiny 1 px centre-point handles — always mounted so ReactFlow can render
// edges between nodes.  They are invisible and never intercept pointer events,
// so they never block the attribute scrollbar.
const GHOST_HANDLE: React.CSSProperties = {
  background: "transparent",
  border: "none",
  borderRadius: 0,
  width: 1,
  height: 1,
  minWidth: 1,
  minHeight: 1,
  pointerEvents: "none",
};

// Full-perimeter overlay handles — always mounted so ReactFlow's handle
// registry stays stable.  Interactivity is toggled via CSS pointerEvents only;
// mounting/unmounting handles mid-session causes ReactFlow to lose the drag
// state and the connection line stops rendering.
function PerimeterHandles({ connecting }: { connecting: boolean }) {
  const pe = connecting ? "auto" : "none";
  const cur = connecting ? "crosshair" : "default";
  const overlay: React.CSSProperties = {
    background: "transparent",
    border: "none",
    borderRadius: 0,
    zIndex: 10,
    pointerEvents: pe,
    cursor: cur,
  };
  return (
    <>
      {/* Ghost handles — always present for edge rendering */}
      <Handle type="source" id="left"   position={Position.Left}   style={GHOST_HANDLE} />
      <Handle type="source" id="right"  position={Position.Right}  style={GHOST_HANDLE} />
      <Handle type="source" id="top"    position={Position.Top}    style={GHOST_HANDLE} />
      <Handle type="source" id="bottom" position={Position.Bottom} style={GHOST_HANDLE} />

      {/* Full-perimeter interactive overlay — always mounted, pointer events
          toggled via CSS so the drag gesture never loses its start point. */}
      <Handle type="source" id="left-i"   position={Position.Left}
        style={{ ...overlay, width: 14, height: "100%", top: 0, left: 0, transform: "none" }} />
      <Handle type="source" id="right-i"  position={Position.Right}
        style={{ ...overlay, width: 14, height: "100%", top: 0, right: 0, left: "auto", transform: "none" }} />
      <Handle type="source" id="top-i"    position={Position.Top}
        style={{ ...overlay, height: 14, width: "100%", left: 0, top: 0, transform: "none" }} />
      <Handle type="source" id="bottom-i" position={Position.Bottom}
        style={{ ...overlay, height: 14, width: "100%", left: 0, bottom: 0, top: "auto", transform: "none" }} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Row and section renderers for the column list.  Extracted so the segmented
// (fact) and flat (dim) layouts share the same leaf row presentation.
// ---------------------------------------------------------------------------
function AttributeRow({ attr, tFn }: { attr: TableAttribute; tFn: (key: string) => string }) {
  const { label: tl, color: tc } = typeInfo(attr.data_type);
  return (
    <div
      title={`${attr.name} · ${attr.data_type}`}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 4,
        padding: "0 6px",
        lineHeight: "14px",
        position: "relative",
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        id={attr.name}
        style={{ ...GHOST_HANDLE, left: -6 }}
      />
      <span
        style={{
          minWidth: 20,
          fontSize: 8,
          fontWeight: 700,
          color: tc,
          fontFamily: font.mono,
          letterSpacing: 0,
          flexShrink: 0,
        }}
      >
        {tl}
      </span>
      <span
        style={{
          fontSize: 10,
          flex: 1,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          color: attr.is_user_defined ? "#6B4C8A" : "#333333",
          fontStyle: attr.is_user_defined ? "italic" : "normal",
        }}
      >
        {attr.is_user_defined ? `${tFn("hierarchies.computedPrefix")}` : ""}
        {attr.name}
      </span>
      <Handle
        type="source"
        position={Position.Right}
        id={attr.name}
        style={{ ...GHOST_HANDLE, right: -6 }}
      />
    </div>
  );
}

const SECTION_HEADER_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 2,
  padding: "2px 4px 2px 2px",
  cursor: "pointer",
  fontSize: 9,
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: 0.2,
  color: "#5A6577",
  background: "#F2F7F4",
  borderTop: "1px solid #CBD5E1",
  userSelect: "none",
};

function OverlayChips({
  counts,
  fg,
  onOpenAggregates,
  onOpenPockets,
  tFn,
}: {
  counts: OverlayCounts;
  fg: string;
  onOpenAggregates: (e: React.MouseEvent) => void;
  onOpenPockets: (e: React.MouseEvent) => void;
  tFn: (key: string, vars?: Record<string, string>) => string;
}) {
  const chipBase: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 2,
    padding: "0 4px",
    height: 13,
    borderRadius: 7,
    fontSize: 8,
    fontWeight: 700,
    lineHeight: "13px",
    background: "rgba(255,255,255,0.24)",
    color: fg,
    cursor: "pointer",
    userSelect: "none",
    flexShrink: 0,
  };
  return (
    <>
      {counts.aggregates > 0 && (
        <Tooltip title={counts.aggregates === 1 ? tFn("erdNode.aggregatesCount", { count: String(counts.aggregates) }) : tFn("erdNode.aggregatesCountPlural", { count: String(counts.aggregates) })}>
          <span
            role="button"
            aria-label={counts.aggregates === 1 ? tFn("erdNode.aggregatesCount", { count: String(counts.aggregates) }) : tFn("erdNode.aggregatesCountPlural", { count: String(counts.aggregates) })}
            style={chipBase}
            onClick={onOpenAggregates}
          >
            <BoltIcon sx={{ fontSize: 9 }} />
            {counts.aggregates}
          </span>
        </Tooltip>
      )}
      {counts.pockets > 0 && (
        <Tooltip title={counts.pockets === 1 ? tFn("erdNode.pocketsCount", { count: String(counts.pockets) }) : tFn("erdNode.pocketsCountPlural", { count: String(counts.pockets) })}>
          <span
            role="button"
            aria-label={counts.pockets === 1 ? tFn("erdNode.pocketsCount", { count: String(counts.pockets) }) : tFn("erdNode.pocketsCountPlural", { count: String(counts.pockets) })}
            style={chipBase}
            onClick={onOpenPockets}
          >
            <ViewCompactIcon sx={{ fontSize: 9 }} />
            {counts.pockets}
          </span>
        </Tooltip>
      )}
    </>
  );
}

function HierarchyGroupsBlock({
  groups,
  t,
}: {
  groups: HierarchyGroupOnTable[];
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  if (groups.length === 0) return null;
  return (
    <div
      style={{
        borderBottom: "1px solid #CBD5E1",
        background: "#f4f8fb",
        padding: "2px 0",
      }}
    >
      {groups.map((g) => (
        <div key={g.name} style={{ padding: "2px 6px" }}>
          <div
            style={{
              fontSize: 9,
              fontWeight: 700,
              textTransform: "uppercase",
              letterSpacing: 0.2,
              color: "#2e5a88",
              lineHeight: "12px",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
            title={t("erdNode.hierarchyLabel", { name: g.name })}
          >
            {t("erdNode.hierarchyLabel", { name: g.name })}
          </div>
          {g.levels.map((lvl) => (
            <div
              key={`${g.name}:${lvl.ordinal}:${lvl.column_name}`}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                padding: "0 2px 0 4px",
                lineHeight: "13px",
              }}
              title={`${lvl.name} · ${lvl.column_name}`}
            >
              <span
                style={{
                  minWidth: 14,
                  height: 13,
                  fontSize: 8,
                  fontWeight: 700,
                  color: "#ffffff",
                  background: "#3A5EA8",
                  borderRadius: 7,
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                  padding: "0 3px",
                }}
              >
                {lvl.ordinal}
              </span>
              <span
                style={{
                  fontSize: 10,
                  color: "#1a2d5a",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  flex: 1,
                }}
              >
                {lvl.column_name}
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function SegmentedList({
  attributes,
  segmentation,
  sectionOpen,
  onToggle,
}: {
  attributes: TableAttribute[];
  segmentation: SegmentationRefs;
  sectionOpen: Record<SegmentKey, boolean>;
  onToggle: (key: SegmentKey) => void;
}) {
  const t = useT();
  const buckets: Record<SegmentKey, TableAttribute[]> = {
    measures: [],
    dimensions: [],
    keys: [],
    levels: [],
    unused: [],
  };
  for (const attr of attributes) {
    buckets[bucketAttribute(attr.name, segmentation)].push(attr);
  }
  return (
    <>
      {SEGMENT_ORDER.map(({ key, label }) => {
        const rows = buckets[key];
        if (rows.length === 0) return null;
        const open = sectionOpen[key];
        const translatedLabel = t(label);
        return (
          <div key={key}>
            <div
              style={SECTION_HEADER_STYLE}
              onClick={(e) => {
                e.stopPropagation();
                onToggle(key);
              }}
              role="button"
              aria-expanded={open}
              aria-label={rows.length === 1 ? t("erdNode.sectionAriaLabel", { label: translatedLabel, count: String(rows.length) }) : t("erdNode.sectionAriaLabelPlural", { label: translatedLabel, count: String(rows.length) })}
            >
              {open ? (
                <ExpandMoreIcon sx={{ fontSize: 12 }} />
              ) : (
                <ChevronRightIcon sx={{ fontSize: 12 }} />
              )}
              <span style={{ flex: 1 }}>{translatedLabel}</span>
              <span
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color: "#5A6577",
                  background: "#ffffff",
                  padding: "0 4px",
                  borderRadius: 6,
                  border: "1px solid #CBD5E1",
                }}
              >
                {rows.length}
              </span>
            </div>
            {/* Always keep AttributeRow handles mounted so ReactFlow edges
                stay anchored when a section is collapsed. Height is zeroed
                out visually; mount/unmount would break the drag state. */}
             <div style={open ? undefined : { height: 0, overflow: "hidden", pointerEvents: "none" }}>
              {rows.map((attr) => <AttributeRow key={attr.id} attr={attr} tFn={t} />)}
            </div>
          </div>
        );
      })}
    </>
  );
}

function ERDTableNode({ id, data }: { id: string; data: ERDNodeData }) {
  const t = useT();
  const {
    table,
    projectId,
    modelId,
    segmentation,
    hierarchyGroups,
    overlay,
    dimmed,
    readOnly,
  } = data;
  const updateNodeInternals = useUpdateNodeInternals();
  const isResized = useStore(
    (s) => s.nodeInternals.get(id)?.style?.height !== undefined,
  );
  const segmentationActive = table.table_type === "fact" && !!segmentation;
  const hasHierarchyOverlay =
    table.table_type !== "fact" &&
    !!hierarchyGroups &&
    hierarchyGroups.length > 0;
  const openPanel        = useBuilderStore((s) => s.openPanel);
  const isConnectingMode = useBuilderStore((s) => s.isConnectingMode);
  const [sectionOpen, setSectionOpen] = useState<Record<SegmentKey, boolean>>(() =>
    loadSectionState(table.id),
  );

  const toggleSection = (key: SegmentKey) => {
    setSectionOpen((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      saveSectionState(table.id, next);
      return next;
    });
  };
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorInitialTab, setEditorInitialTab] = useState<"table-details" | "classification">("table-details");
  const [dataOpen, setDataOpen] = useState(false);
  const [dataPage, setDataPage] = useState(0);
  const [totalRows, setTotalRows] = useState<number | null>(null);
  const [countLoading, setCountLoading] = useState(false);

  const attributes = useQuery({
    queryKey: ["tableAttributes", projectId, modelId, table.id],
    queryFn: () => tableAttributesApi.list(projectId, modelId, table.id),
    staleTime: 20 * 1000,
  });

  // Look up the source's connection id so the unified edit dialog can sync
  // physical columns from the live connection.
  const sources = useSources(projectId, modelId);
  const connectionId =
    sources.data?.find((s) => s.id === table.source_id)?.project_connection_id ?? null;

  useEffect(() => {
    if (attributes.data) {
      // Defer one tick so the per-attribute Handles have mounted before we
      // tell ReactFlow to recompute connection anchors.
      setTimeout(() => updateNodeInternals(id), 0);
    }
  }, [attributes.data, id, updateNodeInternals, sectionOpen]);

  const PAGE_SIZE = 50;

  const previewQuery = useQuery({
    queryKey: ["tablePreview", projectId, modelId, table.id, dataPage],
    queryFn: () => modelTablesApi.preview(projectId, modelId, table.id, dataPage, PAGE_SIZE),
    enabled: dataOpen,
    staleTime: 30 * 1000,
  });

  function openDataDialog() {
    setDataPage(0);
    setTotalRows(null);
    setDataOpen(true);
  }

  async function handleGetCount() {
    setCountLoading(true);
    try {
      const res = await modelTablesApi.preview(projectId, modelId, table.id, 0, 1, true);
      setTotalRows(res.total_rows ?? null);
    } catch {
      // silently ignore — user can retry
    } finally {
      setCountLoading(false);
    }
  }

  const h = nodeHeader[table.table_type] ?? FALLBACK_HEADER;
  const label = table.alias ?? table.display_name;
  const showPhysicalSubtitle =
    table.alias &&
    table.physical_name &&
    table.alias !== table.physical_name;

  return (
    <>
      <NodeResizer
        minWidth={200}
        minHeight={150}
        isVisible={!readOnly}
        lineStyle={{ borderColor: "transparent" }}
        handleStyle={{
          width: 12,
          height: 12,
          borderRadius: 2,
          background: "transparent",
          border: "none",
        }}
        onResizeEnd={(_e, params) => {
          if (readOnly) return;
          window.dispatchEvent(
            new CustomEvent("node-resize-end", {
              detail: { id, w: params.width, h: params.height },
            }),
          );
        }}
      />
      <div
        style={{
          width: "100%",
          height: isResized ? "100%" : "auto",
          display: "flex",
          flexDirection: "column",
          background: "#ffffff",
          border: `1px solid ${h.border}`,
          borderRadius: 2,
          fontSize: 10,
          fontFamily: font.sans,
          boxShadow: "0 1px 2px rgba(0,0,0,0.06)",
          overflow: "hidden",
          position: "relative",
          ...(dimmed
            ? { opacity: 0.35, filter: "grayscale(100%)" }
            : {}),
        }}
        aria-label={dimmed ? t("erdNode.excludedByPersona", { label: label ?? "" }) : (label ?? "")}
        data-testid={`node-${table.physical_name ?? id}`}
      >
        <PerimeterHandles connecting={isConnectingMode} />

        <div
          style={{
            background: h.bg,
            borderBottom: `1px solid ${h.border}`,
            padding: "2px 5px 2px 7px",
            display: "flex",
            alignItems: "center",
            gap: 3,
            color: h.headerTextColor,
          }}
        >
          <span
            role={readOnly ? undefined : "button"}
            title={readOnly ? (label ?? "") : t("erdNode.viewTableDetails")}
            onClick={(e) => {
              e.stopPropagation();
              if (!isConnectingMode && !readOnly) {
                setEditorInitialTab("table-details");
                setEditorOpen(true);
              }
            }}
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
              cursor: isConnectingMode || readOnly ? "default" : "pointer",
            }}
          >
            <span style={{ fontWeight: 600, fontSize: 10, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {label}
            </span>
            {showPhysicalSubtitle && (
              <span style={{ fontWeight: 400, fontSize: 8, opacity: 0.65, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {table.physical_name}
              </span>
            )}
          </span>
          {overlay && (overlay.aggregates > 0 || overlay.pockets > 0) && (
            <OverlayChips
              counts={overlay}
              fg={h.headerTextColor}
              onOpenAggregates={(e) => {
                e.stopPropagation();
                openPanel("aggregates");
              }}
              onOpenPockets={(e) => {
                e.stopPropagation();
                openPanel("pockets");
              }}
              tFn={t}
            />
          )}
          <Tooltip title={t("erdNode.viewData")}>
            <IconButton size="small" aria-label={t("erdNode.viewTableData")} onClick={openDataDialog} sx={{ p: 0.25 }}>
              <TableRowsIcon fontSize="inherit" />
            </IconButton>
          </Tooltip>
          {!readOnly && (
            <Tooltip title={t("erdNode.classify")}>
              <IconButton
                size="small"
                aria-label={t("erdNode.classifyTable")}
                onClick={() => {
                  setEditorOpen(true);
                  setEditorInitialTab("classification");
                }}
                sx={{ p: 0.25 }}
              >
                <AutoFixHighIcon fontSize="inherit" />
              </IconButton>
            </Tooltip>
          )}
          {!readOnly && (
            <Tooltip title={t("erdNode.editTable")}>
              <IconButton
                size="small"
                aria-label={t("erdNode.editTable")}
                onClick={() => {
                  setEditorInitialTab("table-details");
                  setEditorOpen(true);
                }}
                sx={{ p: 0.25 }}
              >
                <EditIcon fontSize="inherit" />
              </IconButton>
            </Tooltip>
          )}
          <span
            style={{
              background: h.badgeBg,
              color: h.badgeColor,
              fontSize: 7,
              fontWeight: 700,
              padding: "1px 3px",
              borderRadius: 2,
              whiteSpace: "nowrap",
              flexShrink: 0,
            }}
          >
            {h.badgeText}
          </span>
        </div>

        {hasHierarchyOverlay && (
          <HierarchyGroupsBlock groups={hierarchyGroups!} t={t} />
        )}
        <div
          style={{
            flex: "1 1 auto",
            minHeight: 0,
            maxHeight: isResized
              ? "none"
              : segmentationActive
                ? 280
                : hasHierarchyOverlay
                  ? 200
                  : 168,
            overflowY: "auto",
            overflowX: "hidden",
            padding: "1px 0",
          }}
        >
          {attributes.isLoading && (
            <div style={{ padding: "6px 10px", color: "#90a4ae", fontSize: 11 }}>
              {t("erdNode.loadingAttributes")}
            </div>
          )}
          {attributes.isError && (
            <div style={{ padding: "4px 10px", color: "#e53935", fontSize: 10 }}>
              {t("erdNode.couldNotLoadAttributes")}
            </div>
          )}
          {attributes.data &&
            (segmentationActive ? (
              <SegmentedList
                attributes={attributes.data}
                segmentation={segmentation!}
                sectionOpen={sectionOpen}
                onToggle={toggleSection}
              />
            ) : (
              attributes.data.map((attr: TableAttribute) => (
                <AttributeRow key={attr.id} attr={attr} tFn={t} />
              ))
            ))}
        </div>
      </div>

      {/* ── View Data dialog ─────────────────────────────────────────── */}
      <Dialog
        open={dataOpen}
        onClose={() => setDataOpen(false)}
        maxWidth="xl"
        fullWidth
        PaperProps={{ sx: { height: "80vh", display: "flex", flexDirection: "column" } }}
      >
        <DialogTitle sx={{ pb: 1 }}>
          <Box display="flex" alignItems="center" gap={1}>
            <TableRowsIcon fontSize="small" />
            <span>{label}</span>
            <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
              {table.physical_name}
            </Typography>
          </Box>
        </DialogTitle>

        <DialogContent sx={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", p: 1 }}>
          {previewQuery.isLoading && (
            <Box display="flex" justifyContent="center" alignItems="center" flex={1}>
              <CircularProgress />
            </Box>
          )}
          {previewQuery.isError && (
            <Alert severity="error">
              {t("erdNode.failedToLoadData", { error: (previewQuery.error as any)?.response?.data?.detail ?? t("erdTableNode.unknownError") })}
            </Alert>
          )}
          {previewQuery.data && (
            <TableContainer sx={{ flex: 1, overflow: "auto" }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    {previewQuery.data.columns.map((col) => (
                      <TableCell
                        key={col}
                        sx={{
                          fontWeight: 700,
                          fontSize: 12,
                          whiteSpace: "nowrap",
                          bgcolor: "grey.100",
                          py: 0.75,
                        }}
                      >
                        {col}
                      </TableCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {previewQuery.data.rows.map((row, ri) => (
                    <TableRow key={ri} hover>
                      {previewQuery.data!.columns.map((col) => {
                        const val = row[col];
                        return (
                          <TableCell
                            key={col}
                            sx={{
                              fontSize: 11,
                              maxWidth: 220,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                              py: 0.5,
                              color: val === null ? "text.disabled" : "text.primary",
                            }}
                          >
                            {val === null ? t("erdTableNode.nullDisplay") : String(val)}
                          </TableCell>
                        );
                      })}
                    </TableRow>
                  ))}
                  {previewQuery.data.rows.length === 0 && (
                    <TableRow>
                      <TableCell
                        colSpan={previewQuery.data.columns.length || 1}
                        align="center"
                        sx={{ color: "text.secondary", py: 3 }}
                      >
                        {t("erdNode.noRowsReturned")}
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </DialogContent>

        <DialogActions sx={{ px: 2, py: 1, borderTop: 1, borderColor: "divider", flexWrap: "wrap", gap: 1 }}>
          {/* Row count section */}
          <Box display="flex" alignItems="center" gap={1} mr="auto">
            {totalRows !== null ? (
              <Typography variant="body2" color="text.secondary">
                {t("erdNode.totalRows", { count: totalRows.toLocaleString() })}
              </Typography>
            ) : (
              <Button
                size="small"
                variant="outlined"
                onClick={handleGetCount}
                disabled={countLoading}
                startIcon={countLoading ? <CircularProgress size={14} /> : undefined}
              >
                {countLoading ? t("erdNode.countingRows") : t("erdNode.getTotalRowCount")}
              </Button>
            )}
          </Box>

          {/* Pagination */}
          <Box display="flex" alignItems="center" gap={1}>
            <Typography variant="caption" color="text.secondary">
              {t("erdNode.pageLabel", { page: String(dataPage + 1) })}
              {previewQuery.data
                ? ` · ${previewQuery.data.rows.length !== 1
                    ? t("erdNode.rowsOnPagePlural", { count: String(previewQuery.data.rows.length) })
                    : t("erdNode.rowsOnPage", { count: String(previewQuery.data.rows.length) })}`
                : ""}
            </Typography>
            <Button
              size="small"
              disabled={dataPage === 0 || previewQuery.isFetching}
              onClick={() => setDataPage((p) => p - 1)}
            >
              {t("erdNode.previous")}
            </Button>
            <Button
              size="small"
              disabled={!previewQuery.data?.has_more || previewQuery.isFetching}
              onClick={() => setDataPage((p) => p + 1)}
            >
              {t("erdNode.next")}
            </Button>
          </Box>

          <Button onClick={() => setDataOpen(false)}>{t("erdNode.close")}</Button>
        </DialogActions>
      </Dialog>

      {/* ── Unified table edit dialog (general / columns / attributes) ─ */}
      {editorOpen && (
        <TableEditDialog
          open={editorOpen}
          onClose={() => setEditorOpen(false)}
          projectId={projectId}
          modelId={modelId}
          sourceId={table.source_id}
          table={table}
          connectionId={connectionId}
          initialTab={editorInitialTab}
        />
      )}
    </>
  );
}

export default memo(ERDTableNode);
