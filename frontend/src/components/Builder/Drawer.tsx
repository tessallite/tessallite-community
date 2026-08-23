import {
  Box,
  Drawer as MuiDrawer,
  IconButton,
  Tooltip,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import KeyboardDoubleArrowLeftIcon from "@mui/icons-material/KeyboardDoubleArrowLeft";
import KeyboardDoubleArrowRightIcon from "@mui/icons-material/KeyboardDoubleArrowRight";
import { useBuilderStore, type PanelId } from "../../store/builderStore";
import { useT } from "../../i18n";
import HelpIconButton from "../HelpIconButton";

// Title i18n keys per drawer panel. Typed as Partial<Record<PanelId, ...>> so
// the compiler rejects any key that is not a real PanelId (catches typos and
// the dead "kpis" entry that previously sat here — F-026-09). Panels without
// an entry fall back to a humanised id at render time.
const PANEL_TITLE_KEYS: Partial<Record<PanelId, string>> = {
  connections: "panels.connections",
  sources: "panels.sourcesTargets",
  joins: "panels.joins",
  hierarchies: "panels.hierarchies",
  dimensions: "panels.dimensions",
  measures: "panels.measures",
  aggregates: "panels.aggregates",
  statistics: "panels.statistics",
  predictive: "drawer.predictiveAggregates",
  lifecycle: "drawer.aggregateLifecycle",
  pockets: "panels.pockets",
  personas: "panels.personas",
  "row-security": "panels.rowSecurity",
  refresh: "drawer.refreshPolicies",
  lineage: "panels.lineage",
  diagnostics: "panels.diagnostics",
  endpoints: "panels.endpoints",
  query: "builder.query",
  "measure-query": "drawer.measureQuery",
  glossary: "panels.glossary",
  scheduler: "panels.scheduler",
  settings: "panels.settings",
  parameters: "panels.parameters",
  "data-quality": "panels.dataQuality",
  "data-tags": "panels.dataTags",
  impact: "panels.impact",
  "impact-analysis": "panels.impactAnalysis",
  "schema-changes": "panels.schemaChanges",
  "named-sets": "panels.namedSets",
  "saved-queries": "panels.savedQueries",
  scratchpad: "panels.scratchpad",
  alerts: "panels.alerts",
  "model-docs": "panels.modelDocs",
};

const PANEL_HELP_LINKS: Partial<Record<PanelId, string>> = {
  connections: "/help/modelling/manage-connections.html",
  sources: "/help/modelling/add-tables-to-a-model.html",
  joins: "/help/modelling/define-joins.html",
  hierarchies: "/help/modelling/define-hierarchies.html",
  dimensions: "/help/modelling/define-dimensions.html",
  measures: "/help/modelling/define-measures.html",
  aggregates: "/help/modelling/configure-aggregates.html",
  statistics: "/help/modelling/source-statistics.html",
  predictive: "/help/modelling/predictive-aggregates.html",
  lifecycle: "/help/modelling/aggregate-lifecycle.html",
  pockets: "/help/modelling/configure-pocket-tables.html",
  personas: "/help/modelling/configure-personas.html",
  "row-security": "/help/modelling/configure-row-security.html",
  refresh: "/help/modelling/run-a-refresh.html",
  lineage: "/help/modelling/view-model-lineage.html",
  diagnostics: "/help/modelling/view-diagnostics.html",
  endpoints: "/help/integrations/api-reference.html",
  query: "/help/modelling/query-panel.html",
  "measure-query": "/help/modelling/measure-query-panel.html",
  glossary: "/help/modelling/business-glossary.html",
  scheduler: "/help/modelling/manage-aggregate-schedules.html",
  settings: "/help/admin/model-configuration.html",
  parameters: "/help/modelling/parameterized-filters.html",
  "data-quality": "/help/modelling/data-quality-rules.html",
  "data-tags": "/help/modelling/data-tags.html",
  impact: "/help/modelling/usage-downstream-assets.html",
  "impact-analysis": "/help/concepts/query-routing.html",
  "schema-changes": "/help/modelling/schema-changes.html",
  "named-sets": "/help/modelling/named-sets.html",
  "saved-queries": "/help/modelling/query-panel.html",
  scratchpad: "/help/modelling/query-panel.html",
  alerts: "/help/admin/alert-configuration.html",
  "model-docs": "/help/modelling/model-details.html",
};

const AGGREGATE_TAB_HELP_LINKS: Record<string, string> = {
  list: "/help/modelling/configure-aggregates.html",
  refresh: "/help/modelling/run-a-refresh.html",
  "smart-builder": "/help/modelling/manage-aggregate-schedules.html",
  predictive: "/help/modelling/predictive-aggregates.html",
  settings: "/help/admin/model-configuration.html",
};

interface Props {
  children?: React.ReactNode;
}

export default function Drawer({ children }: Props) {
  const t = useT();
  const activePanel = useBuilderStore((s) => s.activePanel);
  const closePanel = useBuilderStore((s) => s.closePanel);
  const expanded = useBuilderStore((s) => s.drawerExpanded);
  const toggleExpanded = useBuilderStore((s) => s.toggleDrawerExpanded);
  const aggregateTab = useBuilderStore((s) => s.aggregateTab);

  // The drawer is non-modal so the toolbelt and canvas stay visible and
  // interactive while a panel is open — required for the joins "connecting
  // mode" workflow, where the user draws joins on the canvas with the joins
  // panel open. We keep the `temporary` variant (so the Paper UNMOUNTS when
  // closed — a `persistent` drawer leaves an 820px Paper translated off-screen
  // to the right, which escapes the ancestor overflow:hidden and adds
  // horizontal scroll, pushing the canvas past the window edge). Instead we
  // strip the modal behaviours: no backdrop, no scroll lock, no focus trap,
  // and a click-through root (pointerEvents:none) with an interactive Paper
  // (pointerEvents:auto).
  //
  // Escape-to-close is handled in ONE place — useGlobalShortcuts — which also
  // guards against closing the drawer when a nested MUI dialog is open
  // (F-026-13). A duplicate window listener used to live here and would close
  // the drawer underneath an open dialog on the same keypress; it was removed.

  const activeHelpLink =
    activePanel === "aggregates"
      ? AGGREGATE_TAB_HELP_LINKS[aggregateTab ?? "list"]
      : activePanel
      ? PANEL_HELP_LINKS[activePanel]
      : undefined;

  // Every PanelId has a title key above; if one is ever missing, humanise the
  // id (e.g. "saved-queries" -> "Saved Queries") rather than leaking the raw
  // lowercase id to the user (F-026-09).
  const titleKey = activePanel ? PANEL_TITLE_KEYS[activePanel] : undefined;
  const panelTitle = activePanel
    ? titleKey
      ? t(titleKey)
      : activePanel
          .split("-")
          .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
          .join(" ")
    : "";

  return (
    <MuiDrawer
      anchor="right"
      open={Boolean(activePanel)}
      onClose={closePanel}
      hideBackdrop
      disableEnforceFocus
      disableScrollLock
      // Escape is owned by useGlobalShortcuts (which skips closing when a
      // nested dialog is open — F-026-13). Disable MUI's built-in
      // Escape-to-close so the drawer is not closed a second time, behind an
      // open dialog, on the same keypress.
      disableEscapeKeyDown
      // The modal root spans the viewport; make it click-through so the
      // toolbelt and canvas behind it stay interactive. The Paper re-enables
      // pointer events for the panel itself.
      sx={{ pointerEvents: "none", zIndex: (theme) => theme.zIndex.drawer + 2 }}
      PaperProps={{
        sx: {
          pointerEvents: "auto",
          width: expanded ? "100%" : "min(820px, 100vw)",
        },
      }}
    >
      <Box data-testid={`drawer${activePanel ? `-${activePanel}` : ""}`} sx={{ width: "100%", display: "flex", flexDirection: "column", height: "100%" }}>
        <Box
          sx={{
            px: 2,
            py: 1.5,
            borderBottom: 1,
            borderColor: "divider",
            display: "flex",
            alignItems: "center",
          }}
        >
          <Typography variant="h6" fontWeight={700} flexGrow={1}>
            {panelTitle}
          </Typography>
          {activeHelpLink && (
            <HelpIconButton href={activeHelpLink} sx={{ mr: 0.5 }} />
          )}
          <Tooltip title={expanded ? t("drawer.retractPanel") : t("drawer.expandPanel")}>
            <IconButton size="small" onClick={toggleExpanded} sx={{ mr: 0.5 }}>
              {expanded ? (
                <KeyboardDoubleArrowRightIcon />
              ) : (
                <KeyboardDoubleArrowLeftIcon />
              )}
            </IconButton>
          </Tooltip>
          <IconButton size="small" onClick={closePanel}>
            <CloseIcon />
          </IconButton>
        </Box>
        <Box sx={{ flex: 1, overflow: "auto", p: 2.5 }}>{children}</Box>
      </Box>
    </MuiDrawer>
  );
}
