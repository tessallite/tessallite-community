import { lazy, Suspense, useCallback, useEffect, useRef } from "react";
import { safeLocalGet } from "../utils/safeLocalStorage";
import PanelErrorBoundary from "../components/Builder/PanelErrorBoundary";
import { useT } from "../i18n";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Box, Button, Chip, CircularProgress, FormControlLabel, IconButton, Snackbar, Switch, Tab, Tabs, Tooltip, Typography } from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import SettingsIcon from "@mui/icons-material/Settings";
import HelpIconButton from "../components/HelpIconButton";
import {
  useModel,
  useProject,
  useSources,
  useAllModelTables,
  useTargets,
  useHierarchies,
  useDimensions,
  useMeasures,
  useJoins,
  useAggregates,
  usePockets,
} from "../api/hooks";
import { modelsApi } from "../api/client";
import { useBuilderStore, PANEL_IDS, type PanelId } from "../store/builderStore";
import { useModelEditorStore } from "../store/useModelEditorStore";
import UnsavedChangesGuard from "../components/Builder/UnsavedChangesGuard";
import VersionsDialog from "../components/Builder/VersionsDialog";
import SaveVersionDialog from "../components/Builder/SaveVersionDialog";
import { versionsApi } from "../api/versionsApi";
import ModelImportExportDialog from "../components/importExport/ModelImportExportDialog";
import { useConfirm } from "../components/Confirm";
import { useState } from "react";
import SaveIcon from "@mui/icons-material/Save";
import HistoryIcon from "@mui/icons-material/History";
import RocketLaunchIcon from "@mui/icons-material/RocketLaunch";
import StopCircleIcon from "@mui/icons-material/StopCircleOutlined";
import ImportExportIcon from "@mui/icons-material/ImportExport";
import Canvas from "../components/Builder/Canvas";
import Drawer from "../components/Builder/Drawer";
import MiniTabs from "../components/Builder/MiniTabs";
import ShortcutHelpDialog from "../components/Builder/ShortcutHelpDialog";
import StatusBar from "../components/Builder/StatusBar";
import Toolbelt from "../components/Builder/Toolbelt";
import ValidationTray from "../components/Builder/ValidationTray";
import { useModelValidation } from "../components/Builder/useModelValidation";
import { useGlobalShortcuts } from "../hooks/useGlobalShortcuts";
import { extractApiError } from "../utils/extractApiError";

/**
 * Phase G1 code split — every drawer panel and the Model Health
 * tab are lazy-loaded. Canvas stays eager because it's the
 * default miniTab view; users see it on first navigation.
 * Everything else is pulled in on first interaction with the
 * toolbelt or the mini-tabs.
 */
const ModelHealthPanel = lazy(
  () => import("../components/ModelHealth/ModelHealthPanel"),
);
const UsageAnalyticsTab = lazy(
  () => import("../components/ModelHealth/UsageAnalyticsTab"),
);
const KpiScorecardTab = lazy(
  () => import("../components/ModelHealth/KpiScorecardTab"),
);
const AggregatesPanel = lazy(() => import("../components/Panels/AggregatesPanel"));
const PocketTablesPanel = lazy(() => import("../components/Panels/PocketTablesPanel"));
const PersonasPanel = lazy(() => import("../components/Panels/PersonasPanel"));
const RowSecurityPanel = lazy(() => import("../components/Panels/RowSecurityPanel"));
const ConnectionsPanel = lazy(() => import("../components/Panels/ConnectionsPanel"));
const AlertsPanel = lazy(() => import("../components/Panels/AlertsPanel"));
const DiagnosticsPanel = lazy(() => import("../components/Panels/DiagnosticsPanel"));
const EndpointsPanel = lazy(() => import("../components/Panels/EndpointsPanel"));
const HierarchiesPanel = lazy(() => import("../components/Panels/HierarchiesPanel"));
const DimensionsPanel = lazy(() => import("../components/Panels/DimensionsPanel"));
const JoinsPanel = lazy(() => import("../components/Panels/JoinsPanel"));
const LineagePanel = lazy(() => import("../components/Panels/LineagePanel"));
const GlossaryPanel = lazy(() => import("../components/Panels/GlossaryPanel"));
const MeasuresPanel = lazy(() => import("../components/Panels/MeasuresPanel"));
const NamedSetsPanel = lazy(() => import("../components/Panels/NamedSetsPanel"));
const KpisPanel = lazy(() => import("../components/Panels/KpisPanel"));
const QueryPanel = lazy(() => import("../components/Panels/QueryPanel"));
const SavedQueriesPanel = lazy(() => import("../components/Panels/SavedQueriesPanel"));
const MeasureQueryPanel = lazy(() => import("../components/Panels/MeasureQueryPanel"));
const DataQualityPanel = lazy(() => import("../components/Panels/DataQualityPanel"));
const DataTagsPanel = lazy(() => import("../components/Panels/DataTagsPanel"));
const ImpactPanel = lazy(() => import("../components/Panels/ImpactPanel"));
const ImpactAnalysisPanel = lazy(() => import("../components/Panels/ImpactAnalysisPanel"));
const ModelConfigDrawer = lazy(() => import("../components/Settings/ModelConfigDrawer"));
const SourcesPanel = lazy(() => import("../components/Panels/SourcesPanel"));
const ParametersPanel = lazy(
  () => import("../components/Panels/ParametersPanel"),
);
const SchemaChangesPanel = lazy(
  () => import("../components/Builder/SchemaChangesPanel"),
);
const SchedulerPanel = lazy(() => import("../components/Panels/SchedulerPanel"));
const ScratchpadPanel = lazy(() => import("../components/Panels/ScratchpadPanel"));
const ModelDetailsPanel = lazy(() => import("../components/Panels/ModelDetailsPanel"));

// Panels handled by dedicated ?panel= branches above (settings opens its own
// drawer; the aggregate-family ids route into the Aggregates panel via a tab
// map) — excluded from the generic deep-link allow-list so they aren't opened
// twice. Everything else in the PanelId union is deep-linkable. Deriving the
// set from PANEL_IDS (the single source of truth) instead of hand-maintaining
// a duplicate list keeps named-sets / saved-queries / alerts deep-linkable
// without drift (F-026-10).
const SPECIAL_DEEP_LINK_PANELS = new Set<PanelId>([
  "settings",
  "refresh",
  "predictive",
  "lifecycle",
  "scheduler",
  "statistics",
]);
const DEEP_LINK_PANELS = new Set<PanelId>(
  PANEL_IDS.filter((id) => !SPECIAL_DEEP_LINK_PANELS.has(id)),
);

function PanelFallback() {
  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        p: 4,
      }}
    >
      <CircularProgress size={22} />
    </Box>
  );
}

function QueryTabContent() {
  const t = useT();
  const [mode, setMode] = useState<"pivot" | "freeform">("pivot");

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <Tabs
        value={mode}
        onChange={(_, v) => setMode(v)}
        sx={{
          minHeight: 36,
          px: 2,
          borderBottom: 1,
          borderColor: "divider",
          "& .MuiTab-root": { minHeight: 36, py: 0, textTransform: "none" },
        }}
      >
        <Tab value="pivot" label={t("builder.pivotTable")} />
        <Tab value="freeform" label={t("builder.freeformQuery")} />
      </Tabs>
      <Box sx={{ flex: 1, overflow: "auto", p: mode === "freeform" ? 2 : 0 }}>
        {mode === "pivot" ? (
          <Suspense fallback={<PanelFallback />}>
            <MeasureQueryPanel />
          </Suspense>
        ) : (
          <Suspense fallback={<PanelFallback />}>
            <QueryPanel />
          </Suspense>
        )}
      </Box>
    </Box>
  );
}

function KpisTabContent({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const [sub, setSub] = useState<"dashboard" | "define">("dashboard");

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <Tabs
        value={sub}
        onChange={(_, v) => setSub(v)}
        sx={{
          minHeight: 36,
          px: 2,
          borderBottom: 1,
          borderColor: "divider",
          "& .MuiTab-root": { minHeight: 36, py: 0, textTransform: "none" },
        }}
      >
        <Tab value="dashboard" label={t("builder.kpisDashboard")} />
        <Tab value="define" label={t("builder.kpisDefine")} />
      </Tabs>
      <Box sx={{ flex: 1, overflow: "auto" }}>
        {sub === "dashboard" ? (
          <Suspense fallback={<PanelFallback />}>
            <KpiScorecardTab projectId={projectId} modelId={modelId} />
          </Suspense>
        ) : (
          <Suspense fallback={<PanelFallback />}>
            <KpisPanel />
          </Suspense>
        )}
      </Box>
    </Box>
  );
}

export default function ModelBuilder() {
  const { tenantId, projectId, modelId } = useParams<{
    tenantId: string;
    projectId: string;
    modelId: string;
  }>();
  const resolvedTenantId = tenantId || safeLocalGet("tenant_id", "");

  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const qc = useQueryClient();
  const t = useT();
  const reset = useBuilderStore((s) => s.reset);
  const activePanel = useBuilderStore((s) => s.activePanel);
  const openPanel = useBuilderStore((s) => s.openPanel);
  const miniTab = useBuilderStore((s) => s.miniTab);
  const setAggregateTab = useBuilderStore((s) => s.setAggregateTab);
  const selectedObjectId = useBuilderStore((s) => s.selectedObjectId);
  const selectedObjectType = useBuilderStore((s) => s.selectedObjectType);

  const setEditorModel = useModelEditorStore((s) => s.setModel);
  const clearEditorModel = useModelEditorStore((s) => s.clearModel);

  const setReadOnly = useBuilderStore((s) => s.setReadOnly);

  // Reset builder state on mount / model change
  useEffect(() => {
    reset();
    return () => reset();
  }, [modelId, reset]);

  // Propagate readOnly mode to the store so all panels can check it and
  // disable editing controls. Two independent sources force read-only:
  //  - the ?readonly=1 share-link parameter (Bug-5301);
  //  - a consumer role (model_viewer / viewer) that cannot author this model
  //    (Bug-8101 / F-104-01): the backend model detail returns
  //    caller_can_author=false, so the Model Builder opens read-only and the
  //    SAVE/DEPLOY/authoring controls are hidden. The backend remains
  //    authoritative — this only aligns the UI with the server gate.
  const isShareLinkReadOnly = searchParams.get("readonly") === "1";

  // Honour ?panel=<id> deep-links (e.g. Explorer "Lifecycle log" entry).
  // Runs after the reset effect on mount, opens the requested panel, then
  // strips the param so a manual close doesn't get re-opened by browser nav.
  useEffect(() => {
    const requested = searchParams.get("panel");
    if (!requested) return;
    if (requested === "settings") {
      setSettingsDrawerOpen(true);
    } else if (["refresh", "predictive", "lifecycle", "scheduler", "statistics"].includes(requested)) {
      const tabMap: Record<string, "refresh" | "smart-builder" | "predictive" | "list"> = {
        refresh: "refresh",
        predictive: "predictive",
        scheduler: "smart-builder",
        statistics: "list",
        lifecycle: "list",
      };
      setAggregateTab(tabMap[requested] ?? "list");
      openPanel("aggregates" as PanelId);
    } else if (DEEP_LINK_PANELS.has(requested as PanelId)) {
      openPanel(requested as PanelId);
    }
    if (searchParams.get("tab") === "pivot") {
      openPanel("measure-query" as PanelId);
    }
    const next = new URLSearchParams(searchParams);
    next.delete("panel");
    setSearchParams(next, { replace: true });
  }, [modelId, searchParams, setSearchParams, openPanel]);

  // Reset the editor's dirty/version pointers whenever the model changes.
  // The actual values come from the model fetch a few lines below; we
  // refresh them via setModel once the fetch resolves.
  useEffect(() => {
    if (!modelId) return;
    return () => clearEditorModel();
  }, [modelId, clearEditorModel]);

  // Parallel data fetches
  const project = useProject(projectId!);
  const model = useModel(projectId!, modelId!);
  const sources = useSources(projectId!, modelId!);
  const sourceIds = (sources.data ?? []).map((s) => s.id);
  const allTables = useAllModelTables(projectId!, modelId!, sourceIds);
  const targets = useTargets(projectId!, modelId!);
  const hierarchies = useHierarchies(projectId!, modelId!);
  const dimensions = useDimensions(projectId!, modelId!);
  const measures = useMeasures(projectId!, modelId!);
  const joins = useJoins(projectId!, modelId!);
  const aggregates = useAggregates(projectId!, modelId!);
  const pockets = usePockets(projectId!, modelId!);

  // Bug-8101 / F-104-01: a consumer role that cannot author this model
  // (backend caller_can_author=false) forces the builder read-only, exactly
  // like the ?readonly=1 share link. Undefined (still loading, or a legacy
  // backend that omits the field) does NOT force read-only, so authoring users
  // are never blocked by a slow/absent field — the backend gate still governs
  // every mutation regardless.
  const roleForcesReadOnly = model.data?.caller_can_author === false;
  const isReadOnly = isShareLinkReadOnly || roleForcesReadOnly;
  useEffect(() => {
    setReadOnly(isReadOnly);
  }, [isReadOnly, setReadOnly]);

  // Bug-7405: include allTables.isLoading so the canvas doesn't flash empty
  // while the dependent table query is still in flight.
  const isLoading =
    model.isLoading ||
    sources.isLoading ||
    allTables.isLoading ||
    targets.isLoading ||
    hierarchies.isLoading ||
    dimensions.isLoading ||
    measures.isLoading ||
    joins.isLoading ||
    aggregates.isLoading ||
    pockets.isLoading;

  // Bug-7404: detect when any required query has failed. A completed 4xx/5xx
  // or network failure exits the loading state but leaves the data as
  // undefined -- the ?? [] fallbacks make it indistinguishable from a genuinely
  // empty model. Block the builder and show an error instead.
  const requiredQueries = [model, sources, allTables, targets, hierarchies, dimensions, measures, joins, aggregates, pockets];
  const hasError = requiredQueries.some((q) => q.isError);

  // Validation tray producer (F-026-01): merges the structural validator's
  // model alerts with client-side structural rules into the builder store.
  useModelValidation(projectId!, modelId!, {
    tables: allTables.data ?? [],
    joins: joins.data ?? [],
    hasTarget: (targets.data?.length ?? 0) > 0,
    ready:
      !isLoading &&
      !allTables.isLoading &&
      joins.isSuccess &&
      targets.isSuccess,
  });

  // Hydrate the editor store with the model's current version pointers
  // when the model fetch resolves. The model-service decorates every
  // ModelResponse with last_saved_version_number and deployed_version_number
  // (see _resolve_version_numbers), so this read is authoritative — no
  // follow-up round-trip to /versions is needed to render the toolbar chip.
  // setModel preserves isDirty when the model ID is unchanged, so a
  // background refetch here refreshes the pointers without clearing edits
  // the user has made since the last save.
  useEffect(() => {
    if (!modelId || !model.data) return;
    setEditorModel({
      modelId,
      lastSavedVersion: model.data.last_saved_version_number ?? null,
      deployedVersion: model.data.deployed_version_number ?? null,
      lastDeployedAt: model.data.last_deployed_at ?? null,
    });
  }, [modelId, model.data, setEditorModel]);

  const [shortcutHelpOpen, setShortcutHelpOpen] = useState(false);
  const [settingsDrawerOpen, setSettingsDrawerOpen] = useState(false);
  // Canvas view controls (zoom/fit) — published by the Canvas on init so the
  // keyboard shortcuts can drive it (F-026-08). Held in a ref to avoid
  // re-rendering the page when the canvas mounts.
  const canvasViewControls = useRef<{
    zoomIn: () => void;
    zoomOut: () => void;
    fitView: () => void;
  } | null>(null);
  // Stable identity (Bug-6375): the ref target never changes, so this callback
  // has no reactive deps. A fresh identity each render would churn the Canvas's
  // publish/cleanup effect and null out the live zoom/fit controls.
  const handleViewControlsReady = useCallback(
    (c: { zoomIn: () => void; zoomOut: () => void; fitView: () => void } | null) => {
      canvasViewControls.current = c;
    },
    [],
  );
  const globalMessage = useBuilderStore((s) => s.globalMessage);
  const clearGlobalMessage = useBuilderStore((s) => s.clearGlobalMessage);
  const setGlobalMessage = useBuilderStore((s) => s.setGlobalMessage);
  useGlobalShortcuts({
    openShortcutHelp: () => setShortcutHelpOpen(true),
    focusMiniTabs: () => {
      const el = document.getElementById("model-builder-mini-tabs");
      const firstTab = el?.querySelector<HTMLElement>("[role=\"tab\"]");
      firstTab?.focus();
    },
    onZoomIn: () => canvasViewControls.current?.zoomIn(),
    onZoomOut: () => canvasViewControls.current?.zoomOut(),
    onFitView: () => canvasViewControls.current?.fitView(),
  });

  const modelEnabled = (model.data?.status ?? "active") !== "disabled";
  const toggleModelEnabled = useMutation({
    mutationFn: (enabled: boolean) =>
      modelsApi.update(projectId!, modelId!, { status: enabled ? "active" : "disabled" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["models", projectId] });
    },
    // F-026-07: surface enable/disable failures (e.g. viewer-role 403, network)
    // instead of silently re-enabling the switch with no feedback.
    onError: (err) =>
      setGlobalMessage(
        extractApiError(err, t("modelBuilder.toggleEnabledFailed")),
        "error",
      ),
  });

  if (isLoading) {
    return (
      <Box
        display="flex"
        alignItems="center"
        justifyContent="center"
        height="60vh"
        gap={2}
      >
        <CircularProgress size={28} />
        <Typography>{t("modelBuilder.loading")}</Typography>
      </Box>
    );
  }

  // Bug-7404: block the builder and show an actionable error when any
  // required dataset failed to load, instead of rendering as a valid empty
  // model that masks auth failures, network errors, and 500s.
  if (hasError) {
    return (
      <Box
        display="flex"
        flexDirection="column"
        alignItems="center"
        justifyContent="center"
        height="60vh"
        gap={2}
      >
        <Alert severity="error" sx={{ maxWidth: 600 }}>
          {t("modelBuilder.loadFailed")}
        </Alert>
        <Button
          variant="outlined"
          onClick={() => {
            for (const q of requiredQueries) {
              if (q.isError) q.refetch();
            }
          }}
        >
          {t("modelBuilder.retry")}
        </Button>
      </Box>
    );
  }

  // Summary bar counts
  const sourceCount = sources.data?.length ?? 0;
  const tableCount = allTables.data?.length ?? 0;
  const dimCount = dimensions.data?.length ?? 0;
  const measCount = measures.data?.length ?? 0;
  const hierarchyCount = hierarchies.data?.length ?? 0;
  const joinCount = joins.data?.length ?? 0;
  const aggCount = aggregates.data?.length ?? 0;
  const pocketCount = pockets.data?.length ?? 0;
  const hasTarget = (targets.data?.length ?? 0) > 0;

  return (
    <Box
      sx={{
        display: "flex",
        flexDirection: "column",
        height: "calc(100vh - 80px)",
        overflow: "hidden",
      }}
    >
      {/* Header row -- wraps on narrow viewports (Bug-7406) */}
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          flexWrap: "wrap",
          px: 1.5,
          py: 0.5,
          borderBottom: 1,
          borderColor: "divider",
          gap: 2,
        }}
      >
        <Tooltip title={t("builder.backToProject")}>
          <IconButton size="small" onClick={() => navigate("/")}>
            <ArrowBackIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        <Typography variant="h6" fontWeight={700} noWrap>
          {project.data?.display_name ?? project.data?.slug ?? ""}
          <Typography component="span" variant="h6" fontWeight={400} color="text.secondary" sx={{ mx: 0.75 }}>{t("modelBuilder.projectModelSeparator")}</Typography>
          {model.data?.display_name ?? t("builder.title")}
        </Typography>
        <FormControlLabel
          sx={{ ml: 1 }}
          control={
            <Switch
              size="small"
              checked={modelEnabled}
              onChange={(e) => toggleModelEnabled.mutate(e.target.checked)}
              disabled={toggleModelEnabled.isPending || isReadOnly}
            />
          }
          label={
            <Typography variant="caption" color="text.secondary">
              {t("modelBuilder.modelEnabled")}
            </Typography>
          }
        />
        <MiniTabs />
        <Box sx={{ flexGrow: 1 }} />
        <ModelToolbarActions
          projectId={projectId!}
          projectSlug={project.data?.slug ?? ""}
          modelId={modelId!}
          isDeployed={Boolean(model.data?.deployed_version_id)}
          lastDeployedAt={model.data?.last_deployed_at as string | null | undefined}
          readOnly={isReadOnly}
        />
        <HelpIconButton
          href="/help/modelling/model-canvas-tour.html"
          title={t("builder.helpTitle")}
          sx={{ color: "text.secondary" }}
        />
        <Tooltip title={t("builder.settings")}>
          <IconButton
            size="small"
            onClick={() => setSettingsDrawerOpen(true)}
            sx={{ color: "text.secondary" }}
          >
            <SettingsIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>

      {/* Main area */}
      <Box sx={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* Toolbelt (icon strip) */}
        <Toolbelt />

        {/* Content area */}
        <Box
          sx={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
          <Box sx={{ flex: 1, overflow: "auto" }}>
            {miniTab === "kpi-scorecard" ? (
              <KpisTabContent projectId={projectId!} modelId={modelId!} />
            ) : miniTab === "matrix" ? (
              <Suspense fallback={<PanelFallback />}>
                <ModelHealthPanel
                  projectId={projectId!}
                  modelId={modelId!}
                />
              </Suspense>
            ) : miniTab === "analytics" ? (
              <Suspense fallback={<PanelFallback />}>
                <UsageAnalyticsTab
                  projectId={projectId!}
                  modelId={modelId!}
                />
              </Suspense>
            ) : miniTab === "query" || miniTab === "pivot" ? (
              <QueryTabContent />
            ) : (
              <Canvas
                projectId={projectId!}
                modelId={modelId!}
                tenantSlug={resolvedTenantId}
                projectSlug={project.data?.slug ?? ""}
                modelSlug={model.data?.slug ?? ""}
                versionNumber={model.data?.last_saved_version_number ?? null}
                tables={allTables.data ?? []}
                joins={joins.data ?? []}
                canvasLayout={model.data?.canvas_layout}
                readOnly={isReadOnly}
                onViewControlsReady={handleViewControlsReady}
              />
            )}
          </Box>
          {miniTab === "canvas" ? (
            <>
              <ValidationTray />
              <StatusBar
                tableCount={tableCount}
                joinCount={joinCount}
                dimCount={dimCount}
                measCount={measCount}
                aggCount={aggCount}
                sourceCount={sourceCount}
                hierarchyCount={hierarchyCount}
                pocketCount={pocketCount}
                hasTarget={hasTarget}
                selectedName={
                  selectedObjectId && selectedObjectType
                    ? resolveSelectedName(
                        t,
                        selectedObjectId,
                        selectedObjectType,
                        allTables.data ?? [],
                        joins.data ?? [],
                      )
                    : null
                }
              />
            </>
          ) : null}
        </Box>
      </Box>

      {/* Drawer (context panel) — full-length overlay */}
      <Drawer>
        <PanelErrorBoundary panelName={activePanel ?? t("modelBuilder.panel")} key={activePanel}>
        <Suspense fallback={<PanelFallback />}>
          {activePanel === "connections" && <ConnectionsPanel />}
          {activePanel === "sources" && <SourcesPanel />}
          {activePanel === "joins" && <JoinsPanel />}
          {activePanel === "hierarchies" && <HierarchiesPanel />}
          {activePanel === "dimensions" && <DimensionsPanel />}
          {activePanel === "measures" && <MeasuresPanel />}
          {activePanel === "aggregates" && <AggregatesPanel />}
          {activePanel === "parameters" && <ParametersPanel />}
          {activePanel === "data-quality" && <DataQualityPanel />}
          {activePanel === "pockets" && <PocketTablesPanel />}
          {activePanel === "personas" && <PersonasPanel />}
          {activePanel === "row-security" && <RowSecurityPanel />}
          {activePanel === "data-tags" && <DataTagsPanel />}
          {activePanel === "impact" && <ImpactPanel />}
          {activePanel === "impact-analysis" && <ImpactAnalysisPanel />}
          {activePanel === "lineage" && <LineagePanel />}
          {activePanel === "alerts" && <AlertsPanel />}
          {activePanel === "diagnostics" && <DiagnosticsPanel />}
          {activePanel === "endpoints" && <EndpointsPanel />}
          {activePanel === "query" && <QueryPanel />}
          {activePanel === "measure-query" && <MeasureQueryPanel />}
          {activePanel === "saved-queries" && <SavedQueriesPanel />}
          {activePanel === "glossary" && <GlossaryPanel />}
          {activePanel === "named-sets" && <NamedSetsPanel />}
          {activePanel === "scheduler" && <SchedulerPanel projectId={projectId!} modelId={modelId!} tenantId={resolvedTenantId} />}
          {activePanel === "schema-changes" && <SchemaChangesPanel />}
          {activePanel === "scratchpad" && <ScratchpadPanel />}
          {activePanel === "model-docs" && <ModelDetailsPanel />}
        </Suspense>
        </PanelErrorBoundary>
      </Drawer>

      <UnsavedChangesGuard />
      <ShortcutHelpDialog
        open={shortcutHelpOpen}
        onClose={() => setShortcutHelpOpen(false)}
      />
      <Suspense fallback={null}>
        <ModelConfigDrawer
          projectId={projectId!}
          modelId={modelId!}
          modelName={model.data?.display_name ?? ""}
          open={settingsDrawerOpen}
          onClose={() => setSettingsDrawerOpen(false)}
        />
      </Suspense>

      <Snackbar
        open={!!globalMessage}
        autoHideDuration={6000}
        onClose={clearGlobalMessage}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          severity={globalMessage?.severity ?? "info"}
          onClose={clearGlobalMessage}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {globalMessage?.text}
        </Alert>
      </Snackbar>
    </Box>
  );
}


function tableLabel(
  t: (key: string) => string,
  item: {
    display_name?: string | null;
    alias?: string | null;
    physical_name?: string | null;
  },
): string {
  return item.display_name || item.alias || item.physical_name || t("modelBuilder.unnamed");
}

function resolveSelectedName(
  t: (key: string) => string,
  id: string,
  type: string,
  tables: Array<{
    id: string;
    display_name?: string | null;
    alias?: string | null;
    physical_name?: string | null;
  }>,
  joins: Array<{ id: string; left_table_id: string; right_table_id: string }>,
): string | null {
  if (type === "source") {
    const tbl = tables.find((x) => x.id === id);
    return tbl ? tableLabel(t, tbl) : null;
  }
  if (type === "join") {
    const j = joins.find((x) => x.id === id);
    if (!j) return null;
    const left = tables.find((x) => x.id === j.left_table_id);
    const right = tables.find((x) => x.id === j.right_table_id);
    const fallback = t("modelBuilder.unknown");
    return `${left ? tableLabel(t, left) : fallback} \u2194 ${right ? tableLabel(t, right) : fallback}`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Toolbar actions: Save, Deploy/Undeploy, Versions
// ---------------------------------------------------------------------------

export function ModelToolbarActions({
  projectId,
  projectSlug,
  modelId,
  isDeployed,
  lastDeployedAt,
  readOnly = false,
}: {
  projectId: string;
  projectSlug: string;
  modelId: string;
  isDeployed: boolean;
  lastDeployedAt?: string | null;
  readOnly?: boolean;
}) {
  const navigate = useNavigate();
  const tenantSlug = safeLocalGet("tenant_id", "");
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();
  const isDirty = useModelEditorStore((s) => s.isDirty);
  const lastSavedVersion = useModelEditorStore((s) => s.lastSavedVersion);
  const deployedVersion = useModelEditorStore((s) => s.deployedVersion);
  const markClean = useModelEditorStore((s) => s.markClean);
  const setGlobalMessage = useBuilderStore((s) => s.setGlobalMessage);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [importExportOpen, setImportExportOpen] = useState(false);

  // F-026-07: Save / Deploy / Undeploy are the most important builder actions.
  // A failure (viewer-role 403, expired session, 409 conflict, network) must
  // not be swallowed — the user has to know their version does not exist. Each
  // mutation now reports the server detail (or a translated fallback) through
  // the snackbar channel.
  // F-013-16: the Save API accepts an optional one-line summary; the toolbar
  // now collects it through SaveVersionDialog so the Versions history no
  // longer shows "N/A" for every human save.
  const saveMut = useMutation({
    mutationFn: (summary?: string) => versionsApi.create(projectId, modelId, summary),
    onSuccess: (v) => {
      markClean({ lastSavedVersion: v.version_number });
      qc.invalidateQueries({ queryKey: ["versions", projectId, modelId] });
    },
    onError: (err) =>
      setGlobalMessage(extractApiError(err, t("modelBuilder.saveFailed")), "error"),
  });
  const [saveDialogOpen, setSaveDialogOpen] = useState(false);

  const deployMut = useMutation({
    mutationFn: () => versionsApi.deploy(projectId, modelId),
    onSuccess: (data) => {
      // F-026-13: deploy publishes the last-saved version, but DeployResponse
      // carries no version number, so advance the deployed pointer to the
      // last-saved version optimistically. Without this the chip compares a
      // stale deployedVersion against lastSavedVersion and shows "deployed
      // outdated" until useModel refetches — a modeller may Deploy twice or
      // think Deploy failed. Read the freshest saved version from the store so
      // a save-before-deploy is reflected (avoids a stale closure).
      const savedVersion = useModelEditorStore.getState().lastSavedVersion;
      markClean({
        lastDeployedAt: data.last_deployed_at,
        ...(savedVersion != null ? { deployedVersion: savedVersion } : {}),
      });
      qc.invalidateQueries({ queryKey: ["models", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["versions", projectId, modelId] });
    },
    onError: (err) =>
      setGlobalMessage(extractApiError(err, t("modelBuilder.deployFailed")), "error"),
  });

  const undeployMut = useMutation({
    mutationFn: () => versionsApi.undeploy(projectId, modelId),
    onSuccess: () => {
      markClean({ deployedVersion: null });
      qc.invalidateQueries({ queryKey: ["models", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["versions", projectId, modelId] });
    },
    onError: (err) =>
      setGlobalMessage(extractApiError(err, t("modelBuilder.undeployFailed")), "error"),
  });

  async function handleDeploy() {
    if (isDirty) return; // button is disabled in this case
    if (lastSavedVersion === null) {
      const ok = await confirm({
        title: t("modelBuilder.saveBeforeDeployTitle"),
        message: t("modelBuilder.saveBeforeDeployMessage"),
        confirmLabel: t("modelBuilder.saveAndDeploy"),
        destructive: false,
      });
      if (!ok) return;
      // mutateAsync rejects on failure; the mutations' onError already shows
      // the message, so swallow the rejection here to avoid an unhandled
      // promise rejection (F-026-07). If Save fails, do not attempt Deploy.
      try {
        await saveMut.mutateAsync(undefined);
        await deployMut.mutateAsync();
      } catch {
        // surfaced via onError handlers
      }
      return;
    }
    deployMut.mutate();
  }

  async function handleUndeploy() {
    const ok = await confirm({
      mode: "typed-name",
      title: t("modelBuilder.undeployTitle"),
      message: t("modelBuilder.undeployMessageFull"),
      confirmText: t("modelBuilder.undeploy"),
      confirmLabel: t("builder.undeploy"),
    });
    if (ok) undeployMut.mutate();
  }

  const layoutFlushMut = useMutation({
    mutationFn: () => {
      const flushFn = useBuilderStore.getState().flushCanvasLayoutNow;
      if (flushFn) return flushFn();
      return Promise.reject(new Error("Canvas is not active"));
    },
    onSuccess: () => {
      setGlobalMessage(t("versions.layoutSaved"), "success");
    },
    onError: (err) => {
      setGlobalMessage(extractApiError(err, t("builder.layoutSaveFailed")), "error");
    },
  });

  const saveDisabled = readOnly || saveMut.isPending || layoutFlushMut.isPending;
  const deployDisabled = readOnly || isDirty || deployMut.isPending;
  const isStaleDeployment =
    isDeployed &&
    !isDirty &&
    deployedVersion != null &&
    lastSavedVersion != null &&
    deployedVersion < lastSavedVersion;

  return (
    <>
      <Tooltip
        title={
          isStaleDeployment
            ? t("modelBuilder.deployedOutdatedTooltip", { deployed: String(deployedVersion), saved: String(lastSavedVersion) })
            : isDirty
              ? t("modelBuilder.editedTooltip")
              : lastSavedVersion
                ? t("modelBuilder.savedVersionTooltip", { version: String(lastSavedVersion) })
                : t("modelBuilder.emptyModelTooltip")
        }
      >
        <Chip
          size="small"
          variant="outlined"
          color={isStaleDeployment ? "warning" : isDirty ? "warning" : "default"}
          onClick={isStaleDeployment ? () => setVersionsOpen(true) : undefined}
          sx={isStaleDeployment ? { cursor: "pointer" } : undefined}
          label={
            isDeployed
              ? isDirty
                ? `${t("builder.statusDeployed")} v${deployedVersion ?? "?"} \u00b7 ${t("builder.statusEdited").toLowerCase()}`
                : isStaleDeployment
                  ? `${t("builder.statusDeployed")} v${deployedVersion} \u00b7 v${lastSavedVersion} ${t("builder.statusSaved").toLowerCase()}`
                  : `${t("builder.statusDeployed")} v${deployedVersion ?? "?"}`
              : isDirty
                ? t("builder.statusEdited")
                : lastSavedVersion
                  ? `${t("builder.statusSaved")} v${lastSavedVersion}`
                  : t("builder.statusEmpty")
          }
        />
      </Tooltip>
      <Tooltip title={t("builder.saveTooltip")}>
        <span>
          <IconButton
            size="small"
            onClick={() => setSaveDialogOpen(true)}
            disabled={saveDisabled}
            color="primary"
            aria-label={t("builder.saveModelVersion")}
            data-testid="btn-save"
          >
            <SaveIcon fontSize="small" />
          </IconButton>
        </span>
      </Tooltip>
      {isDeployed ? (
        <Tooltip title={`${t("builder.undeploy")}${lastDeployedAt ? t("modelBuilder.deployed", { when: new Date(lastDeployedAt).toLocaleString() }) : ""}`}>
          <IconButton
            size="small"
            onClick={handleUndeploy}
            color="warning"
            disabled={readOnly || undeployMut.isPending}
            aria-label={t("builder.undeploy")}
            data-testid="btn-undeploy"
          >
            <StopCircleIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      ) : (
        <Tooltip title={isDirty ? t("modelBuilder.saveBeforeDeployingTooltip") : t("modelBuilder.deployLatestTooltip")}>
          <span>
            <IconButton
              size="small"
              onClick={handleDeploy}
              disabled={deployDisabled}
              color="success"
              aria-label={t("builder.deployLatest")}
              data-testid="btn-deploy"
            >
              <RocketLaunchIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      )}
      <Tooltip title={t("builder.versionsTooltip")}>
        <IconButton size="small" onClick={() => setVersionsOpen(true)}>
          <HistoryIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      {/* F-026-04: import mutates the model, so the import/export entry point
          must respect read-only share-link mode like Save/Deploy/Undeploy
          rather than staying always enabled. */}
      {!readOnly && (
      <Tooltip title={t("modelBuilder.importExport")}>
        <IconButton
          size="small"
          onClick={() => setImportExportOpen(true)}
          aria-label={t("modelBuilder.importExport")}
        >
          <ImportExportIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      )}
      <VersionsDialog
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        projectId={projectId}
        modelId={modelId}
      />
      <SaveVersionDialog
        open={saveDialogOpen}
        busy={saveMut.isPending || layoutFlushMut.isPending}
        isDirty={isDirty}
        onClose={() => setSaveDialogOpen(false)}
        onSave={async (mode, summary) => {
          setSaveDialogOpen(false);
          if (mode === "layout") {
            layoutFlushMut.mutate();
          } else {
            // Flush pending canvas layout before snapshotting so the version
            // captures the latest node positions (not a stale debounce state).
            const flushFn = useBuilderStore.getState().flushCanvasLayoutNow;
            if (flushFn) {
              try { await flushFn(); } catch { /* non-fatal for version save */ }
            }
            saveMut.mutate(summary);
          }
        }}
      />
      <ModelImportExportDialog
        open={importExportOpen}
        onClose={() => setImportExportOpen(false)}
        projectId={projectId}
        projectSlug={projectSlug}
        onModelImported={(newModelId) =>
          navigate(
            `/tenants/${tenantSlug}/projects/${projectId}/models/${newModelId}`,
          )
        }
      />
    </>
  );
}
