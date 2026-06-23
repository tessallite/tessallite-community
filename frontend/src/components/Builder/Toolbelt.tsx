import { useState } from "react";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { Box, ButtonBase, IconButton, Tooltip, Typography } from "@mui/material";
import TableChartIcon from "@mui/icons-material/TableChart";
import LinkIcon from "@mui/icons-material/Link";
import AccountTreeIcon from "@mui/icons-material/AccountTree";
import LabelIcon from "@mui/icons-material/Label";
import FunctionsIcon from "@mui/icons-material/Functions";
import BoltIcon from "@mui/icons-material/Bolt";
import ViewCompactIcon from "@mui/icons-material/ViewCompact";
import GroupIcon from "@mui/icons-material/Group";
import LockIcon from "@mui/icons-material/Lock";
import TimelineIcon from "@mui/icons-material/Timeline";
import ApiIcon from "@mui/icons-material/Api";
import CableIcon from "@mui/icons-material/Cable";
import MenuBookIcon from "@mui/icons-material/MenuBook";
import SyncProblemIcon from "@mui/icons-material/SyncProblem";
import ScheduleIcon from "@mui/icons-material/Schedule";
import TuneIcon from "@mui/icons-material/Tune";
import FactCheckIcon from "@mui/icons-material/FactCheck";
import NotificationsIcon from "@mui/icons-material/Notifications";
import TrackChangesIcon from "@mui/icons-material/TrackChanges";
import PlaylistAddCheckIcon from "@mui/icons-material/PlaylistAddCheck";
import StyleIcon from "@mui/icons-material/Style";
import BookmarkIcon from "@mui/icons-material/Bookmark";
import VerifiedIcon from "@mui/icons-material/Verified";
import ScienceIcon from "@mui/icons-material/Science";
import DescriptionIcon from "@mui/icons-material/Description";
import ChevronLeftIcon from "@mui/icons-material/ChevronLeft";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import { useBuilderStore, type PanelId } from "../../store/builderStore";
import { useT } from "../../i18n";

interface ToolItem {
  panel: PanelId;
  labelKey: string;
  icon: React.ReactNode;
}

const TOOLS: ToolItem[] = [
  { panel: "connections", labelKey: "panels.connections", icon: <CableIcon fontSize="small" /> },
  { panel: "sources", labelKey: "panels.sourcesTargets", icon: <TableChartIcon fontSize="small" /> },
  { panel: "joins", labelKey: "panels.joins", icon: <LinkIcon fontSize="small" /> },
  { panel: "hierarchies", labelKey: "panels.hierarchies", icon: <AccountTreeIcon fontSize="small" /> },
  { panel: "dimensions", labelKey: "panels.dimensions", icon: <LabelIcon fontSize="small" /> },
  { panel: "measures", labelKey: "panels.measures", icon: <FunctionsIcon fontSize="small" /> },
  { panel: "named-sets", labelKey: "panels.namedSets", icon: <PlaylistAddCheckIcon fontSize="small" /> },
  { panel: "aggregates", labelKey: "panels.aggregates", icon: <BoltIcon fontSize="small" /> },
  { panel: "pockets", labelKey: "panels.pockets", icon: <ViewCompactIcon fontSize="small" /> },
  { panel: "personas", labelKey: "panels.personas", icon: <GroupIcon fontSize="small" /> },
  { panel: "row-security", labelKey: "panels.rowSecurity", icon: <LockIcon fontSize="small" /> },
  { panel: "lineage", labelKey: "panels.lineage", icon: <TimelineIcon fontSize="small" /> },
  { panel: "impact", labelKey: "panels.impact", icon: <TrackChangesIcon fontSize="small" /> },
  { panel: "endpoints", labelKey: "panels.endpoints", icon: <ApiIcon fontSize="small" /> },
  { panel: "saved-queries", labelKey: "panels.savedQueries", icon: <BookmarkIcon fontSize="small" /> },
  { panel: "glossary", labelKey: "panels.glossary", icon: <MenuBookIcon fontSize="small" /> },
  { panel: "scheduler", labelKey: "panels.scheduler", icon: <ScheduleIcon fontSize="small" /> },
  { panel: "parameters", labelKey: "panels.parameters", icon: <TuneIcon fontSize="small" /> },
  { panel: "data-quality", labelKey: "panels.dataQuality", icon: <VerifiedIcon fontSize="small" /> },
  { panel: "data-tags", labelKey: "panels.dataTags", icon: <StyleIcon fontSize="small" /> },
  { panel: "schema-changes", labelKey: "panels.schemaChanges", icon: <SyncProblemIcon fontSize="small" /> },
  { panel: "scratchpad", labelKey: "panels.scratchpad", icon: <ScienceIcon fontSize="small" /> },
  { panel: "model-docs", labelKey: "panels.modelDocs", icon: <DescriptionIcon fontSize="small" /> },
];

const BOTTOM_TOOLS: ToolItem[] = [
  { panel: "alerts", labelKey: "panels.alerts", icon: <NotificationsIcon fontSize="small" /> },
  { panel: "diagnostics", labelKey: "panels.diagnostics", icon: <FactCheckIcon fontSize="small" /> },
];

const STORAGE_KEY = "builder.toolbelt.expanded";

export default function Toolbelt() {
  const activePanel       = useBuilderStore((s) => s.activePanel);
  const openPanel         = useBuilderStore((s) => s.openPanel);
  const closePanel        = useBuilderStore((s) => s.closePanel);
  const isConnectingMode  = useBuilderStore((s) => s.isConnectingMode);
  const setConnectingMode = useBuilderStore((s) => s.setConnectingMode);
  const t = useT();
  const [expanded, setExpanded] = useState(
    () => safeLocalGet(STORAGE_KEY, "false") === "true",
  );

  function toggleExpanded() {
    setExpanded((prev) => {
      const next = !prev;
      localStorage.setItem(STORAGE_KEY, String(next));
      return next;
    });
  }

  function handleClick(panel: PanelId) {
    if (panel === "joins") {
      // Joins tool toggles connection drawing mode.
      if (isConnectingMode) {
        setConnectingMode(false);
        closePanel();
      } else {
        setConnectingMode(true);
        openPanel("joins");
      }
      return;
    }
    // While connection drawing mode is active, all other tools are locked out.
    if (isConnectingMode) return;
    // Scheduler panel opens directly (no longer redirects to aggregates)
    // The Smart Builder section is still accessible via the Aggregates panel tabs.
    if (activePanel === panel) {
      closePanel();
    } else {
      openPanel(panel);
    }
  }

  function renderTool(tool: ToolItem) {
    const label = t(tool.labelKey);
    const isActive = activePanel === tool.panel;
    // Dim all tools when connection mode is on; Joins stays highlighted.
    const disabled = isConnectingMode && tool.panel !== "joins";
    const connectActive = isConnectingMode && tool.panel === "joins";

    return (
      <Tooltip
        key={tool.panel}
        title={expanded ? "" : disabled ? "" : label}
        placement="right"
      >
        <ButtonBase
          onClick={() => handleClick(tool.panel)}
          aria-label={label}
          data-testid={`tool-${tool.panel}`}
          disabled={disabled}
          sx={{
            display: "flex",
            alignItems: "center",
            gap: 0.75,
            px: 0.75,
            py: 0.5,
            borderRadius: 1,
            justifyContent: "flex-start",
            width: "100%",
            minHeight: 32,
            color: connectActive
              ? "warning.dark"
              : isActive
              ? "primary.main"
              : "text.secondary",
            bgcolor: connectActive
              ? "warning.50"
              : isActive
              ? "primary.50"
              : "transparent",
            opacity: disabled ? 0.35 : 1,
            border: connectActive ? "1px solid" : "1px solid transparent",
            borderColor: connectActive ? "warning.main" : "transparent",
            "&:hover": {
              bgcolor: disabled
                ? "transparent"
                : connectActive
                ? "warning.50"
                : isActive
                ? "primary.50"
                : "action.hover",
            },
            transition: "background-color 150ms, opacity 150ms",
          }}
        >
          <Box sx={{ display: "flex", alignItems: "center", justifyContent: "center", width: 24, flexShrink: 0 }}>
            {tool.icon}
          </Box>
          <Typography
            variant="caption"
            noWrap
            sx={{
              fontWeight: connectActive || isActive ? 600 : 400,
              lineHeight: 1.2,
              width: expanded ? "auto" : 0,
              overflow: "hidden",
              opacity: expanded ? 1 : 0,
              transition: "opacity 200ms ease, width 200ms ease",
            }}
          >
            {label}
          </Typography>
        </ButtonBase>
      </Tooltip>
    );
  }

  return (
    <Box
      data-testid="toolbelt"
      sx={{
        display: "flex",
        flexDirection: "column",
        gap: 0.25,
        p: 0.5,
        pb: 0,
        borderRight: 1,
        borderColor: "divider",
        bgcolor: "grey.50",
        width: expanded ? 168 : 40,
        transition: "width 200ms ease",
        overflow: "hidden",
        flexShrink: 0,
      }}
    >
      <Box
        sx={{
          display: "flex",
          flexDirection: "column",
          gap: 0.25,
          overflowY: expanded ? "auto" : "hidden",
          flexGrow: 1,
          pb: 0.5,
          scrollbarWidth: "none",
          "&:hover": {
            scrollbarWidth: "thin",
            scrollbarColor: "rgba(0,0,0,0.22) transparent",
          },
          "&::-webkit-scrollbar": {
            width: 0,
          },
          "&::-webkit-scrollbar-thumb": {
            backgroundColor: "transparent",
          },
          "&:hover::-webkit-scrollbar": {
            width: 5,
          },
          "&:hover::-webkit-scrollbar-thumb": {
            backgroundColor: "rgba(0,0,0,0.25)",
            borderRadius: 3,
          },
        }}
      >
        {TOOLS.map(renderTool)}
        <Box sx={{ flexGrow: 1 }} />
        {BOTTOM_TOOLS.map(renderTool)}
      </Box>
      <Tooltip title={expanded ? t("builder.collapse") : t("builder.expand")} placement="right">
        <IconButton
          size="small"
          onClick={toggleExpanded}
          sx={{ alignSelf: "center", color: "text.secondary", mb: 0.5, flexShrink: 0 }}
        >
          {expanded ? <ChevronLeftIcon fontSize="small" /> : <ChevronRightIcon fontSize="small" />}
        </IconButton>
      </Tooltip>
    </Box>
  );
}
