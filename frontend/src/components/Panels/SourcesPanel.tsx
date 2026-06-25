import { useEffect, useDeferredValue, useMemo, useRef, useState } from "react";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  Collapse,
  CircularProgress,
  Divider,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  IconButton,
  InputAdornment,
  InputLabel,
  Link,
  List,
  ListItem,
  ListItemText,
  MenuItem,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import BarChartIcon from "@mui/icons-material/BarChart";
import CalendarMonthIcon from "@mui/icons-material/CalendarMonth";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import EditIcon from "@mui/icons-material/Edit";
import ClearIcon from "@mui/icons-material/Clear";
import CallSplitIcon from "@mui/icons-material/CallSplit";
import DeleteIcon from "@mui/icons-material/Delete";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import SearchIcon from "@mui/icons-material/Search";
import TableChartIcon from "@mui/icons-material/TableChart";
import TableRowsIcon from "@mui/icons-material/TableRows";
import {
  connectionsApi,
  modelTablesApi,
  optimizerApiClient,
  sourcesApi,
  dimensionsApi,
  measuresApi,
  tableAttributesApi,
} from "../../api/client";
import { useConnections, useSources } from "../../api/hooks";
import CalendarTableDialog from "../CalendarTableDialog";
import StatisticsPanel from "./StatisticsPanel";
import TableEditDialog, { type TableEditTab } from "./TableEditDialog";
import AliasMapDialog from "./AliasMapDialog";
import TargetPanel from "./TargetPanel";
import DataPreviewPanel from "../Builder/DataPreviewPanel";
import { useConfirm } from "../Confirm";
import { useBuilderStore } from "../../store/builderStore";
import { ui } from "../../theme/tokens";
import type {
  Connection,
  ModelTable,
  ModelTableCreate,
  ProfiledTable,
  SourceCreate,
} from "../../api/types";
import { useT } from "../../i18n";

/* ------------------------------------------------------------------ */
/* Discovered table from the backend                                  */
/* ------------------------------------------------------------------ */
interface DiscoveredTable {
  schema: string;
  table: string;
  type: string;
}

function defaultSchemaValue(sourceType: string) {
  if (sourceType === "postgresql" || sourceType === "redshift") return "public";
  // Hadoop/Spark Hive default namespace — also accept the legacy "jdbc"
  // label for connections created before Phase C unification.
  if (sourceType === "hadoop_spark" || sourceType === "jdbc") return "default";
  if (sourceType === "snowflake") return "PUBLIC";
  if (sourceType === "sqlserver") return "dbo";
  return "";
}

function schemaLabel(sourceType: string) {
  return sourceType === "bigquery" ? "Dataset" : "Schema";
}

function getSourceSchema(config?: Record<string, unknown>, sourceType?: string) {
  const value = config?.schema ?? config?.dataset;
  if (typeof value !== "string") return null;
  if (sourceType === "bigquery" && value.includes(".")) {
    return value.split(".").pop()!;
  }
  return value;
}

function storedLowCardinalityThreshold(): number | undefined {
  const raw = safeLocalGet("builder.settings.lowCardinalityThreshold", "");
  if (!raw) return undefined;
  const n = parseInt(raw, 10);
  return isNaN(n) || n <= 0 ? undefined : n;
}

/* ------------------------------------------------------------------ */
/* Table type display helpers                                         */
/* ------------------------------------------------------------------ */
const TABLE_TYPE_LABELS: Record<string, string> = {
  fact: "tableType.fact",
  dim_aggregate: "tableType.dimAggregate",
  dim_detail: "tableType.dimDetail",
  unclassified: "tableType.unclassified",
  calendar: "tableType.calendar",
};
function tableTypeLabel(type: string, t: (key: string) => string) {
  const key = TABLE_TYPE_LABELS[type];
  return key ? t(key) : type;
}

/* ------------------------------------------------------------------ */
/* Table discovery + classification component                         */
/* ------------------------------------------------------------------ */
function SourceTables({
  projectId,
  modelId,
  sourceId,
  sourceType,
  connectionId,
  initialSchema,
  autoDiscover = false,
}: {
  projectId: string;
  modelId: string;
  sourceId: string;
  sourceType: string;
  connectionId: string;
  initialSchema?: string;
  autoDiscover?: boolean;
}) {
  const t = useT();
  const qc = useQueryClient();
  const [showDiscover, setShowDiscover] = useState(autoDiscover);
  const [schemaFilter, setSchemaFilter] = useState(initialSchema ?? "");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [profileResults, setProfileResults] = useState<ProfiledTable[] | null>(null);
  const [classifyStep, setClassifyStep] = useState(0); // 0=select, 1=review
  const [editDialog, setEditDialog] = useState<{ table: ModelTable; initialTab: TableEditTab } | null>(null);
  const [previewTable, setPreviewTable] = useState<ModelTable | null>(null);
  const [createAliasDialog, setCreateAliasDialog] = useState<{ table: ModelTable } | null>(null);
  const [newAlias, setNewAlias] = useState("");
  const [newAliasDisplayName, setNewAliasDisplayName] = useState("");
  const [tableSearch, setTableSearch] = useState("");
  const deferredSearch = useDeferredValue(tableSearch);
  const [duplicateWarning, setDuplicateWarning] = useState<{
    duplicates: { physicalName: string; existingAlias: string }[];
    pendingAction: "add" | "classify";
  } | null>(null);
  const [showAutoAnalyzePrompt, setShowAutoAnalyzePrompt] = useState(false);
  const [pendingAutoAnalyzeTables, setPendingAutoAnalyzeTables] = useState<{ id: string; physicalName: string }[]>([]);
  const focusedTableId  = useBuilderStore((s) => s.focusedTableId);
  const clearFocusedTable = useBuilderStore((s) => s.clearFocusedTable);
  // Bug-5301: gate mutating controls in the table list under readOnly mode.
  const stReadOnly = useBuilderStore((s) => s.readOnly);
  const rowRefs = useRef<Record<string, HTMLLIElement | null>>({});

  useEffect(() => {
    setSchemaFilter(initialSchema ?? "");
  }, [initialSchema]);

  const tables = useQuery({
    queryKey: ["modelTables", projectId, modelId, sourceId],
    queryFn: () => modelTablesApi.list(projectId, modelId, sourceId),
  });

  const filteredTables = useMemo(() => {
    const all = tables.data ?? [];
    if (!deferredSearch) return all;
    const q = deferredSearch.toLowerCase();
    return all.filter(
      (t) =>
        t.display_name.toLowerCase().includes(q) ||
        t.physical_name.toLowerCase().includes(q) ||
        (t.alias && t.alias.toLowerCase().includes(q)),
    );
  }, [tables.data, deferredSearch]);

  // Canvas → Sources focus enhancement: when a node click sets focusedTableId
  // and that table belongs to this source, scroll the row into view and clear
  // the highlight after a short delay.
  useEffect(() => {
    if (!focusedTableId) return;
    const ownsRow = (tables.data ?? []).some((t) => t.id === focusedTableId);
    if (!ownsRow) return;
    const el = rowRefs.current[focusedTableId];
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    const timer = window.setTimeout(() => clearFocusedTable(), 1500);
    return () => window.clearTimeout(timer);
  }, [focusedTableId, tables.data, clearFocusedTable]);

  const [discoverKey, setDiscoverKey] = useState(autoDiscover ? 1 : 0);
  const [committedSchema, setCommittedSchema] = useState(initialSchema ?? "");
  const discovery = useQuery({
    queryKey: ["discoverTables", projectId, connectionId, committedSchema, discoverKey],
    queryFn: () =>
      connectionsApi.discoverTables(projectId, connectionId, committedSchema || undefined),
    enabled: showDiscover && discoverKey > 0,
  });

  function triggerDiscover() {
    setShowDiscover(true);
    const schema = initialSchema ?? "";
    setSchemaFilter(schema);
    setCommittedSchema(schema);
    setSelected(new Set());
    setProfileResults(null);
    setClassifyStep(0);
    setDiscoverKey((k) => k + 1);
  }

  function runDiscovery() {
    setCommittedSchema(schemaFilter);
    setSelected(new Set());
    setProfileResults(null);
    setClassifyStep(0);
    setDiscoverKey((k) => k + 1);
  }

  function toggleTable(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function findDuplicates(selectedNames: string[]) {
    const existing = tables.data ?? [];
    const dupes: { physicalName: string; existingAlias: string }[] = [];
    for (const name of selectedNames) {
      const match = existing.find((t) => t.physical_name === name);
      if (match) dupes.push({ physicalName: name, existingAlias: match.alias });
    }
    return dupes;
  }

  // Profile/classify selected tables
  const profileMutation = useMutation({
    mutationFn: () => {
      const toProfile = Array.from(selected).map((key) => {
        const [schema, ...rest] = key.split(".");
        return { schema, table: rest.join(".") };
      });
      return connectionsApi.profileTables(projectId, connectionId, toProfile);
    },
    onSuccess: (data) => {
      setProfileResults(data);
      setClassifyStep(1);
    },
  });

  // Add tables with classification + auto-create dimensions/measures
  const addClassified = useMutation({
    mutationFn: async () => {
      if (!profileResults) return;
      const errors: string[] = [];

      for (const profiled of profileResults) {
        const physName = `${profiled.schema}.${profiled.table}`;
        let tbl: { id: string } | undefined;
        try {
          tbl = await modelTablesApi.create(projectId, modelId, sourceId, {
            source_id: sourceId,
            table_type: profiled.classification,
            physical_name: physName,
            display_name: profiled.table,
          });

          await tableAttributesApi.syncColumns(
            projectId,
            modelId,
            tbl.id,
            profiled.columns.map((c) => ({
              column_name: c.column_name,
              data_type: c.data_type,
              is_nullable: true,
            })),
          );

          for (const col of profiled.columns) {
            if (col.suggested_role === "measure") {
              await measuresApi
                .create(projectId, modelId, {
                  name: col.column_name,
                  display_name: col.column_name.replace(/_/g, " "),
                  source_table_id: tbl.id,
                  source_column_name: col.column_name,
                  default_agg: (col.suggested_agg as "sum" | "avg" | "count" | "min" | "max") ?? "sum",
                  data_type: col.data_type,
                  is_additive: true,
                })
                .catch(() => {
                  /* ignore duplicates */
                });
            } else if (
              col.suggested_role === "dimension" ||
              col.suggested_role === "time_dimension"
            ) {
              await dimensionsApi
                .create(projectId, modelId, {
                  name: col.column_name,
                  display_name: col.column_name.replace(/_/g, " "),
                  source_table_id: tbl.id,
                  source_column_name: col.column_name,
                  data_type: col.data_type,
                  is_time_dim: col.suggested_role === "time_dimension",
                })
                .catch(() => {
                  /* ignore duplicates */
                });
            }
          }
        } catch (err: any) {
          if (tbl) {
            await modelTablesApi
              .delete(projectId, modelId, sourceId, tbl.id)
              .catch((e: unknown) => console.warn("Source panel operation failed:", e));
          }
          const detail = err?.response?.data?.detail;
          const reason = typeof detail === "string" ? detail : err?.message ?? "unknown error";
          errors.push(`${profiled.table} (${reason})`);
        }
      }

      await qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      await qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
      await qc.refetchQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      await qc.refetchQueries({ queryKey: ["allModelTables", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      optimizerApiClient
        .refreshSourceStatistics(sourceId, undefined, storedLowCardinalityThreshold())
        .then(() => { qc.invalidateQueries({ queryKey: ["source-statistics", sourceId] }); })
        .catch((e) => { console.warn("auto-refresh source statistics failed:", e); });

      if (errors.length > 0) {
        throw new Error(
          `Failed to add: ${errors.join(", ")}. Successfully added tables were kept.`,
        );
      }
    },
    onSuccess: () => {
      setShowDiscover(false);
      setSelected(new Set());
      setProfileResults(null);
      setClassifyStep(0);
    },
  });

  // Simple add without profiling. Creates every selected table, then
  // discovers and syncs columns. If column discovery returns empty or fails,
  // the table is deleted to avoid orphan zero-column rows.
  const [addTablesWarnings, setAddTablesWarnings] = useState<string[]>([]);
  const addTables = useMutation({
    mutationFn: async () => {
      const createErrors: string[] = [];
      const colWarnings: string[] = [];
      const addedTables: { id: string; physicalName: string }[] = [];

      for (const physicalName of Array.from(selected)) {
        const parts = physicalName.split(".");
        const schema = parts[0] ?? "";
        const tableName = parts.slice(1).join(".");
        const displayName = parts[parts.length - 1] || schema;
        const data: ModelTableCreate = {
          source_id: sourceId,
          table_type: "unclassified",
          physical_name: physicalName,
          display_name: displayName,
        };
        let tbl: { id: string } | undefined;
        try {
          tbl = await modelTablesApi.create(projectId, modelId, sourceId, data);
        } catch (err: any) {
          const detail =
            err?.response?.data?.detail ??
            err?.message ??
            "unknown error";
          createErrors.push(`${displayName}: ${detail}`);
          continue;
        }

        try {
          const columns = await connectionsApi.discoverColumns(
            projectId,
            connectionId,
            schema,
            tableName,
          );
          if (columns && columns.length > 0) {
            await tableAttributesApi.syncColumns(projectId, modelId, tbl.id, columns);
            addedTables.push({ id: tbl.id, physicalName });
          } else {
            await modelTablesApi
              .delete(projectId, modelId, sourceId, tbl.id)
              .catch((e: unknown) => console.warn("Source panel operation failed:", e));
            colWarnings.push(displayName);
          }
        } catch {
          await modelTablesApi
            .delete(projectId, modelId, sourceId, tbl.id)
            .catch((e: unknown) => console.warn("Source panel operation failed:", e));
          colWarnings.push(displayName);
        }
      }

      await qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      await qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
      await qc.refetchQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      await qc.refetchQueries({ queryKey: ["allModelTables", projectId, modelId] });
      optimizerApiClient
        .refreshSourceStatistics(sourceId, undefined, storedLowCardinalityThreshold())
        .then(() => { qc.invalidateQueries({ queryKey: ["source-statistics", sourceId] }); })
        .catch((e) => { console.warn("auto-refresh source statistics failed:", e); });

      if (createErrors.length > 0) {
        throw new Error(createErrors.join("\n"));
      }
      return { colWarnings, addedTables };
    },
    onSuccess: (result) => {
      const { colWarnings, addedTables } = result ?? {};
      setAddTablesWarnings(colWarnings ?? []);
      if (!colWarnings?.length) {
        setSelected(new Set());
        if (addedTables && addedTables.length > 0) {
          setPendingAutoAnalyzeTables(addedTables);
          setShowAutoAnalyzePrompt(true);
        } else {
          setShowDiscover(false);
        }
      }
    },
  });

  const autoAnalyzeMutation = useMutation({
    mutationFn: async () => {
      if (pendingAutoAnalyzeTables.length === 0) return;
      const toProfile = pendingAutoAnalyzeTables.map((t) => {
        const parts = t.physicalName.split(".");
        return { schema: parts[0] ?? "", table: parts.slice(1).join(".") };
      });
      const profiled = await connectionsApi.profileTables(projectId, connectionId, toProfile);
      const existing = tables.data ?? [];
      for (const pt of profiled) {
        const physName = `${pt.schema}.${pt.table}`;
        const match = existing.find((tbl) => tbl.physical_name === physName)
          ?? pendingAutoAnalyzeTables.find((tbl) => tbl.physicalName === physName);
        if (!match) continue;
        const tblId = "id" in match ? match.id : (match as { id: string }).id;
        for (const col of pt.columns) {
          if (col.suggested_role === "measure") {
            await measuresApi.create(projectId, modelId, {
              name: col.column_name,
              display_name: col.column_name.replace(/_/g, " "),
              source_table_id: tblId,
              source_column_name: col.column_name,
              default_agg: (col.suggested_agg as "sum" | "avg" | "count" | "min" | "max") ?? "sum",
              data_type: col.data_type,
              is_additive: true,
            }).catch((e: unknown) => console.warn("Source panel operation failed:", e));
          } else if (col.suggested_role === "dimension" || col.suggested_role === "time_dimension") {
            await dimensionsApi.create(projectId, modelId, {
              name: col.column_name,
              display_name: col.column_name.replace(/_/g, " "),
              source_table_id: tblId,
              source_column_name: col.column_name,
              data_type: col.data_type,
              is_time_dim: col.suggested_role === "time_dimension",
            }).catch((e: unknown) => console.warn("Source panel operation failed:", e));
          }
        }
        if (pt.classification && pt.classification !== "unclassified") {
          await modelTablesApi.update(projectId, modelId, sourceId, tblId, {
            table_type: pt.classification as "fact" | "dim_aggregate" | "dim_detail" | "unclassified",
          }).catch((e: unknown) => console.warn("Source panel operation failed:", e));
        }
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      setPendingAutoAnalyzeTables([]);
      setShowAutoAnalyzePrompt(false);
      setShowDiscover(false);
    },
  });

  function handleAddWithDuplicateCheck(action: "add" | "classify") {
    const names = Array.from(selected);
    const dupes = findDuplicates(names);
    if (dupes.length > 0) {
      setDuplicateWarning({ duplicates: dupes, pendingAction: action });
    } else if (action === "add") {
      addTables.mutate();
    } else {
      profileMutation.mutate();
    }
  }

  function confirmDuplicateAdd() {
    if (!duplicateWarning) return;
    if (duplicateWarning.pendingAction === "add") {
      addTables.mutate();
    } else {
      profileMutation.mutate();
    }
    setDuplicateWarning(null);
  }

  const createAlias = useMutation({
    mutationFn: async () => {
      if (!createAliasDialog) return;
      const t = createAliasDialog.table;
      const aliasTrim = newAlias.trim();
      const displayTrim = newAliasDisplayName.trim();
      await modelTablesApi.create(projectId, modelId, sourceId, {
        source_id: sourceId,
        table_type: t.table_type as "fact" | "dim_aggregate" | "dim_detail" | "unclassified",
        physical_name: t.physical_name,
        alias: aliasTrim || undefined,
        display_name: displayTrim || aliasTrim || t.display_name,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
      setCreateAliasDialog(null);
      setNewAlias("");
      setNewAliasDisplayName("");
    },
  });

  const deleteTable = useMutation({
    mutationFn: (tableId: string) =>
      modelTablesApi.delete(projectId, modelId, sourceId, tableId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
    },
  });

  const updateTableType = useMutation({
    mutationFn: ({ tableId, type }: { tableId: string; type: string }) =>
      modelTablesApi.update(projectId, modelId, sourceId, tableId, {
        table_type: type as "fact" | "dim_aggregate" | "dim_detail" | "unclassified",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
    },
  });

  const confirmTbl = useConfirm();
  async function handleDeleteTable(tbl: { id: string; physical_name?: string; display_name?: string }) {
    const label = tbl.display_name || tbl.physical_name || "this table";
    const ok = await confirmTbl({
      mode: "typed-name",
      title: t("sources.deleteTableTitle"),
      message: (
        <span>
          {t("sources.deleteTableMessage", { name: label })}
        </span>
      ),
      confirmText: label,
      confirmLabel: t("sources.deleteTableLabel"),
    });
    if (ok) deleteTable.mutate(tbl.id);
  }

  // Re-classify existing tables: re-profile and update table_type
  const reclassify = useMutation({
    mutationFn: async () => {
      const existing = tables.data ?? [];
      if (existing.length === 0) return;
      const toProfile = existing.map((t: ModelTable) => {
        const parts = t.physical_name.split(".");
        return { schema: parts[0] ?? "public", table: parts.slice(1).join(".") || parts[0] };
      });
      const profiled = await connectionsApi.profileTables(projectId, connectionId, toProfile);
      // Update each table's type if the classification changed.
      // Per-table errors (e.g. 409 when a second fact is detected) are skipped
      // so one table's constraint does not abort the whole batch.
      for (const pt of profiled) {
        const physName = `${pt.schema}.${pt.table}`;
        const match = existing.find((t: ModelTable) => t.physical_name === physName);
        if (!match || match.table_type === pt.classification) continue;
        try {
          await modelTablesApi.update(projectId, modelId, sourceId, match.id, {
            table_type: pt.classification as "fact" | "dim_aggregate" | "dim_detail" | "unclassified",
          });
        } catch {
          // Skip this table; do not abort the batch
        }
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
    },
  });

  // Show all discovered tables — dimension tables can be added multiple
  // times with different aliases (dimension aliases).
  const availableTables = discovery.data ?? [];

  return (
    <Box sx={{ mt: 0.5 }}>
      {/* Existing tables */}
      {tables.isLoading && <CircularProgress size={14} sx={{ ml: 1 }} />}
      {(tables.data ?? []).length > 0 && !stReadOnly && (
        <Box display="flex" alignItems="center" gap={0.5} sx={{ ml: 1, mb: 0.25 }}>
          <Button
            size="small"
            startIcon={<AutoFixHighIcon />}
            onClick={() => reclassify.mutate()}
            disabled={reclassify.isPending}
          >
            {reclassify.isPending ? <CircularProgress size={14} /> : t("sources.autoReclassify")}
          </Button>
          {reclassify.isError && (
            <Typography variant="caption" color="error">{t("sources.reclassifyFailed")}</Typography>
          )}
          {reclassify.isSuccess && (
            <Typography variant="caption" color="success.main">{t("sources.reclassifySuccess")}</Typography>
          )}
        </Box>
      )}
      {(tables.data ?? []).length > 3 && (
        <Box sx={{ mx: 1, mb: 0.5 }}>
          <TextField
            size="small"
            fullWidth
            placeholder={t("sources.filterTablesPlaceholder")}
            value={tableSearch}
            onChange={(e) => setTableSearch(e.target.value)}
            InputProps={{
              startAdornment: (
                <InputAdornment position="start">
                  <SearchIcon fontSize="small" sx={{ color: "text.secondary" }} />
                </InputAdornment>
              ),
              endAdornment: tableSearch ? (
                <InputAdornment position="end">
                  <IconButton size="small" onClick={() => setTableSearch("")}>
                    <ClearIcon fontSize="small" />
                  </IconButton>
                </InputAdornment>
              ) : null,
            }}
            sx={{ "& .MuiInputBase-root": { height: 32 } }}
          />
          {deferredSearch && (
            <Typography variant="caption" color="text.secondary" sx={{ ml: 0.5 }}>
              {t("sources.showingFiltered", {
                filtered: String(filteredTables.length),
                total: String((tables.data ?? []).length),
              })}
            </Typography>
          )}
        </Box>
      )}
      {filteredTables.length > 0 && (
        <List dense disablePadding>
          {filteredTables.map((table: ModelTable) => (
            <ListItem
              key={table.id}
              ref={(el) => {
                rowRefs.current[table.id] = el;
              }}
              onClick={() => setEditDialog({ table: table, initialTab: "table-details" })}
              sx={{
                py: 0,
                pl: 1,
                cursor: "pointer",
                transition: "background-color 1.2s ease-out",
                bgcolor: focusedTableId === table.id ? "primary.lighter" : "transparent",
                "&:hover": { bgcolor: focusedTableId === table.id ? "primary.lighter" : "action.hover" },
                ...(focusedTableId === table.id && {
                  outline: "2px solid",
                  outlineColor: "primary.main",
                  borderRadius: 1,
                }),
              }}
              secondaryAction={
                <Box display="flex" gap={0.25} alignItems="center">
                  <Tooltip title={t("sources.previewDataTooltip")}>
                    <IconButton
                      size="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        setPreviewTable(table);
                      }}
                    >
                      <TableRowsIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("sources.editTableTooltip")}>
                    <IconButton
                      size="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditDialog({ table: table, initialTab: "table-details" });
                      }}
                    >
                      <EditIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  {!stReadOnly && table.table_type !== "fact" && table.table_type !== "calendar" && (
                    <Tooltip title={t("sources.createAliasTooltip")}>
                      <IconButton
                        size="small"
                        onClick={(e) => {
                          e.stopPropagation();
                          setNewAlias("");
                          setNewAliasDisplayName("");
                          setCreateAliasDialog({ table: table });
                        }}
                      >
                        <CallSplitIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                  {!stReadOnly && (
                    <IconButton
                      size="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeleteTable(table);
                      }}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  )}
                </Box>
              }
            >
              <TableChartIcon fontSize="small" sx={{ mr: 0.5, color: "text.secondary" }} />
              <ListItemText
                primary={
                  <Box component="span" display="flex" gap={0.5} alignItems="center">
                    <span>{table.display_name}</span>
                    {table.alias !== table.display_name && table.alias !== table.physical_name.split(".").pop() && (
                      <Typography variant="caption" component="span" fontWeight={600} sx={{ color: ui.green }}>
                        {t("sources.asAliasLabel", { alias: table.alias })}
                      </Typography>
                    )}
                  </Box>
                }
                secondary={
                  <Box component="span" display="flex" gap={0.5} alignItems="center">
                    <FormControl
                      size="small"
                      variant="standard"
                      onClick={(e) => e.stopPropagation()}
                      sx={{ minWidth: 90 }}
                    >
                      <Select
                        value={table.table_type}
                        disableUnderline
                        disabled={stReadOnly || table.table_type === "calendar"}
                        onChange={(e) => {
                          e.stopPropagation();
                          updateTableType.mutate({ tableId: table.id, type: e.target.value });
                        }}
                        sx={{ fontSize: 10, height: 18, "& .MuiSelect-select": { py: 0, pl: 0 } }}
                      >
                        {table.table_type === "calendar" && (
                          <MenuItem value="calendar" sx={{ fontSize: 12 }}>{tableTypeLabel("calendar", t)}</MenuItem>
                        )}
                        <MenuItem value="unclassified" sx={{ fontSize: 12 }}>{tableTypeLabel("unclassified", t)}</MenuItem>
                        <MenuItem value="fact" sx={{ fontSize: 12 }}>{tableTypeLabel("fact", t)}</MenuItem>
                        <MenuItem value="dim_aggregate" sx={{ fontSize: 12 }}>{tableTypeLabel("dim_aggregate", t)}</MenuItem>
                        <MenuItem value="dim_detail" sx={{ fontSize: 12 }}>{tableTypeLabel("dim_detail", t)}</MenuItem>
                      </Select>
                    </FormControl>
                    <span>·</span>
                    <span>{table.alias}</span>
                    {table.alias !== table.physical_name.split(".").pop() && (
                      <span style={{ opacity: 0.6 }}>({table.physical_name})</span>
                    )}
                  </Box>
                }
                primaryTypographyProps={{ variant: "body2", component: "div" }}
                secondaryTypographyProps={{ variant: "caption", component: "div" }}
              />
            </ListItem>
          ))}
        </List>
      )}

      {/* Discover button */}
      {!showDiscover && !stReadOnly ? (
        <Button size="small" startIcon={<AddIcon />} onClick={triggerDiscover} sx={{ ml: 1, mt: 0.5 }}>
          {t("sources.addTablesButton")}
        </Button>
      ) : !showDiscover ? null : (
        <Box sx={{ px: 1, py: 1, bgcolor: ui.mutedBg, borderRadius: 1, mt: 0.5 }}>
          {/* Step indicator for classify flow */}
          {classifyStep === 1 && profileResults ? (
            /* Classification review */
            <Box>
              <Typography variant="subtitle2" fontWeight={700} mb={1}>
                {t("sources.classifyResultsTitle")}
              </Typography>
              <Typography variant="caption" color="text.secondary" mb={1} display="block">
                {t("sources.classifyResultsDesc")}
              </Typography>
              {profileResults.map((pt) => (
                <Card key={`${pt.schema}.${pt.table}`} variant="outlined" sx={{ mb: 1 }}>
                  <CardContent sx={{ py: 1, "&:last-child": { pb: 1 } }}>
                    <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                      <TableChartIcon fontSize="small" />
                      <Typography variant="body2" fontWeight={600}>
                        {pt.table}
                      </Typography>
                      <FormControl size="small" sx={{ minWidth: 130 }}>
                        <Select
                          value={pt.classification}
                          onChange={(e) => {
                            const next = e.target.value as ProfiledTable["classification"];
                            setProfileResults((prev) =>
                              prev
                                ? prev.map((p) =>
                                    p.schema === pt.schema && p.table === pt.table
                                      ? { ...p, classification: next }
                                      : p,
                                  )
                                : prev,
                            );
                          }}
                          sx={{ height: 24, fontSize: 12 }}
                        >
                          <MenuItem value="unclassified">{t("tableType.unclassified")}</MenuItem>
                          <MenuItem value="fact">{t("tableType.fact")}</MenuItem>
                          <MenuItem value="dim_aggregate">{t("tableType.dimAggregate")}</MenuItem>
                          <MenuItem value="dim_detail">{t("tableType.dimDetail")}</MenuItem>
                          <MenuItem value="calendar">{t("tableType.calendar")}</MenuItem>
                        </Select>
                      </FormControl>
                      <Typography variant="caption" color="text.secondary" sx={{ ml: "auto" }}>
                        {pt.row_count?.toLocaleString() ?? "?"}{" "}{t("sources.rows")}
                      </Typography>
                    </Box>
                    <Typography variant="caption" color="text.secondary">
                      {pt.schema} --{" "}
                      {pt.columns.filter((c) => c.suggested_role === "measure").length}{" "}{t("sources.measures")},{" "}
                      {pt.columns.filter((c) => c.suggested_role === "dimension").length}{" "}{t("sources.dimensions")},{" "}
                      {pt.columns.filter((c) => c.suggested_role === "time_dimension").length}{" "}{t("sources.timeDims")}
                    </Typography>
                    {pt.cardinality_available === false && (
                      <Alert severity="info" sx={{ mt: 0.5, py: 0, fontSize: 12 }}>
                        {t("sources.cardinalityUnavailable")}
                      </Alert>
                    )}
                    <Box sx={{ mt: 0.5, maxHeight: 180, overflow: "auto" }}>
                      {/* Column header */}
                      <Box
                        display="flex"
                        alignItems="center"
                        gap={0.5}
                        sx={{ py: 0.25, borderBottom: 1, borderColor: "divider", mb: 0.25 }}
                      >
                        <Typography variant="caption" fontWeight={700} sx={{ minWidth: 130 }}>
                          {t("sources.columnHeader")}
                        </Typography>
                        <Typography variant="caption" fontWeight={700} sx={{ minWidth: 80 }}>
                          {t("sources.typeColumnHeader")}
                        </Typography>
                        <Typography variant="caption" fontWeight={700} sx={{ minWidth: 70 }}>
                          {t("sources.roleColumnHeader")}
                        </Typography>
                        <Typography variant="caption" fontWeight={700} sx={{ minWidth: 50 }}>
                          {t("sources.distinctColumnHeader")}
                        </Typography>
                      </Box>
                      {pt.columns.map((col) => (
                        <Box
                          key={col.column_name}
                          display="flex"
                          alignItems="center"
                          gap={0.5}
                          sx={{ py: 0.15 }}
                        >
                          <Typography variant="caption" sx={{ minWidth: 130 }} noWrap>
                            {col.column_name}
                          </Typography>
                          <Typography variant="caption" color="text.secondary" sx={{ minWidth: 80 }} noWrap>
                            {col.data_type}
                          </Typography>
                          <Typography component="span" variant="caption" sx={{
                            px: 0.5, py: 0.125, borderRadius: 0.5, fontSize: 10, fontWeight: 500, minWidth: 70, display: "inline-block", textAlign: "center",
                            bgcolor: col.suggested_role === "measure" ? ui.goldBg : col.suggested_role === "time_dimension" ? ui.greenBg : ui.mutedBg,
                            color: col.suggested_role === "measure" ? ui.goldDark : col.suggested_role === "time_dimension" ? ui.green : ui.muted,
                          }}>
                            {col.suggested_role === "measure" && col.suggested_agg ? `${col.suggested_role} (${col.suggested_agg})` : col.suggested_role.replace("_", " ")}
                          </Typography>
                          <Typography variant="caption" color="text.secondary" sx={{ minWidth: 50 }}>
                            {col.approx_distinct != null
                              ? `${col.approx_distinct.toLocaleString()}${
                                  col.cardinality_ratio != null
                                    ? ` (${(col.cardinality_ratio * 100).toFixed(1)}%)`
                                    : ""
                                }`
                              : "--"}
                          </Typography>
                        </Box>
                      ))}
                    </Box>
                  </CardContent>
                </Card>
              ))}
              {(() => {
                const existingFacts = (tables.data ?? []).filter((t) => t.table_type === "fact").length;
                const newFacts = (profileResults ?? []).filter((p) => p.classification === "fact").length;
                return (existingFacts + newFacts) > 1 ? (
                  <Alert severity="warning" sx={{ mb: 1 }}>
                    {t("sources.multipleFactsWarning", { count: String(existingFacts + newFacts) })}
                  </Alert>
                ) : null;
              })()}
              <Box display="flex" gap={1}>
                <Button
                  size="small"
                  variant="contained"
                  startIcon={<CheckCircleIcon />}
                  onClick={() => addClassified.mutate()}
                  disabled={addClassified.isPending}
                >
                  {addClassified.isPending ? (
                    <CircularProgress size={16} />
                  ) : (
                    t("sources.confirmAndAdd")
                  )}
                </Button>
                <Button size="small" onClick={() => { setClassifyStep(0); setProfileResults(null); }}>
                  {t("common.back")}
                </Button>
                <Button size="small" onClick={() => setShowDiscover(false)}>
                  {t("common.cancel")}
                </Button>
              </Box>
              {addClassified.isError && (
                <Alert severity="error" sx={{ mt: 0.5 }}>
                  {(() => {
                    const err = addClassified.error as
                      | { response?: { data?: { detail?: unknown } }; message?: string }
                      | undefined;
                    const detail = err?.response?.data?.detail;
                    if (typeof detail === "string" && detail) return detail;
                    if (err?.message) return err.message;
                    return t("sources.classifyFailed");
                  })()}
                </Alert>
              )}
            </Box>
          ) : (
            /* Table selection step */
            <Box>
              <Box display="flex" gap={1} alignItems="center" mb={1}>
                <TextField
                  label={t("sources.schemaFilterLabel")}
                  size="small"
                  value={schemaFilter}
                  onChange={(e) => setSchemaFilter(e.target.value)}
                  placeholder={sourceType === "bigquery" ? "my_dataset" : "public"}
                  sx={{ flex: 1 }}
                />
                <Button
                  size="small"
                  variant="outlined"
                  onClick={runDiscovery}
                  disabled={discovery.isFetching}
                >
                  {discovery.isFetching ? <CircularProgress size={16} /> : t("sources.scanButton")}
                </Button>
              </Box>

              {discovery.isError && (
                <Alert severity="error" sx={{ mb: 1 }}>
                  {(() => {
                    const err = discovery.error as
                      | { response?: { data?: { detail?: unknown } }; message?: string }
                      | undefined;
                    const detail = err?.response?.data?.detail;
                    if (typeof detail === "string" && detail) return detail;
                    if (err?.message) return err.message;
                    return t("sources.discoverError");
                  })()}
                </Alert>
              )}
              {discovery.isSuccess && availableTables.length === 0 && (
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                  {t("sources.noNewTablesFound")}
                </Typography>
              )}
              {availableTables.length > 0 && (
                <>
                  <Box display="flex" alignItems="center" gap={1}>
                    <Typography variant="caption" color="text.secondary">
                      {t(
                        availableTables.length === 1
                          ? "sources.tablesFoundSingular"
                          : "sources.tablesFoundPlural",
                        { count: String(availableTables.length) },
                      )}
                      {selected.size > 0 && ` — ${t("sources.tablesSelected", { count: String(selected.size) })}`}
                    </Typography>
                    <FormControlLabel
                      sx={{ ml: "auto", mr: 0 }}
                      control={
                        <Checkbox
                          size="small"
                          checked={availableTables.length > 0 && selected.size === availableTables.length}
                          indeterminate={selected.size > 0 && selected.size < availableTables.length}
                          onChange={() => {
                            if (selected.size === availableTables.length) {
                              setSelected(new Set());
                            } else {
                              setSelected(new Set(availableTables.map((d: DiscoveredTable) => `${d.schema}.${d.table}`)));
                            }
                          }}
                        />
                      }
                      label={<Typography variant="caption">{t("sources.selectAll")}</Typography>}
                    />
                  </Box>
                  <Box
                    sx={{
                      maxHeight: 240,
                      overflow: "auto",
                      border: 1,
                      borderColor: "divider",
                      borderRadius: 1,
                      mt: 0.5,
                      mb: 1,
                    }}
                  >
                    {availableTables.map((d: DiscoveredTable) => {
                      const key = `${d.schema}.${d.table}`;
                      return (
                        <FormControlLabel
                          key={key}
                          sx={{ display: "flex", mx: 0, px: 1, py: 0, "&:hover": { bgcolor: "action.hover" } }}
                          control={
                            <Checkbox size="small" checked={selected.has(key)} onChange={() => toggleTable(key)} />
                          }
                          label={
                            <Box>
                              <Typography variant="body2">{d.table}</Typography>
                              <Typography variant="caption" color="text.secondary">
                                {d.schema} -- {d.type}
                              </Typography>
                            </Box>
                          }
                        />
                      );
                    })}
                  </Box>
                </>
              )}
              <Box display="flex" gap={1}>
                <Button
                  size="small"
                  variant="contained"
                  startIcon={<AutoFixHighIcon />}
                  onClick={() => handleAddWithDuplicateCheck("classify")}
                  disabled={selected.size === 0 || profileMutation.isPending}
                  sx={{ bgcolor: ui.purple, "&:hover": { bgcolor: ui.purple, filter: "brightness(1.15)" } }}
                >
                    {profileMutation.isPending ? (
                      <CircularProgress size={16} />
                    ) : (
                      t(
                        selected.size === 1
                          ? "sources.autoClassifyButtonSingular"
                          : "sources.autoClassifyButtonPlural",
                        { count: String(selected.size) },
                      )
                    )}
                </Button>
                <Button
                  size="small"
                  variant="outlined"
                  onClick={() => handleAddWithDuplicateCheck("add")}
                  disabled={selected.size === 0 || addTables.isPending}
                >
                  {addTables.isPending ? <CircularProgress size={16} /> : t("sources.addWithoutClassify")}
                </Button>
                <Button size="small" onClick={() => setShowDiscover(false)}>
                  {t("common.cancel")}
                </Button>
              </Box>
              {profileMutation.isError && (
                <Alert severity="error" sx={{ mt: 0.5 }}>
                  {t("sources.profileFailed")}
                </Alert>
              )}
              {addTables.isError && (
                <Alert severity="error" sx={{ mt: 0.5 }}>
                  {(addTables.error as any)?.message ?? t("sources.addFailed")}
                </Alert>
              )}
              {addTablesWarnings.length > 0 && (
                <Alert severity="error" sx={{ mt: 0.5 }}>
                  {t("sources.columnDiscoveryFailed", { tables: addTablesWarnings.join(", ") })}
                </Alert>
              )}
            </Box>
          )}
        </Box>
      )}

      {editDialog && (
        <TableEditDialog
          open={!!editDialog}
          onClose={() => setEditDialog(null)}
          projectId={projectId}
          modelId={modelId}
          sourceId={sourceId}
          table={editDialog.table}
          connectionId={connectionId}
          initialTab={editDialog.initialTab}
        />
      )}

      {previewTable && (
        <DataPreviewPanel
          open={!!previewTable}
          onClose={() => setPreviewTable(null)}
          projectId={projectId}
          modelId={modelId}
          tableId={previewTable.id}
          tableName={previewTable.display_name || previewTable.alias}
        />
      )}

      <Dialog
        open={!!createAliasDialog}
        onClose={() => setCreateAliasDialog(null)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("sources.createAliasDialogTitle")}</DialogTitle>
        <DialogContent>
          {createAliasDialog && (
            <Stack spacing={1.5} sx={{ mt: 1 }}>
              <Typography variant="caption" color="text.secondary">
                {t("sources.createAliasDescription", { physicalName: createAliasDialog.table.physical_name })}{" "}
                <Link
                  href="/help/modelling/dimension-aliases.html"
                  target="_blank"
                  rel="noopener"
                >
                  {t("sources.learnMore")}
                </Link>
              </Typography>
              <TextField
                label={t("sources.aliasLabel")}
                size="small"
                fullWidth
                value={newAlias}
                onChange={(e) => setNewAlias(e.target.value)}
                helperText={t("sources.aliasHelperText")}
                autoFocus
              />
              <TextField
                label={t("sources.aliasDisplayNameLabel")}
                size="small"
                fullWidth
                value={newAliasDisplayName}
                onChange={(e) => setNewAliasDisplayName(e.target.value)}
                helperText={t("sources.aliasDisplayNameHelperText")}
              />
              {createAlias.isError && (
                <Alert severity="error">
                  {(createAlias.error as any)?.response?.data?.detail ??
                    t("sources.createAliasFailed")}
                </Alert>
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateAliasDialog(null)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => createAlias.mutate()}
            disabled={!newAlias.trim() || createAlias.isPending}
          >
            {createAlias.isPending ? <CircularProgress size={16} /> : t("common.create")}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Duplicate table warning dialog (Bug-388) */}
      <Dialog
        open={!!duplicateWarning}
        onClose={() => setDuplicateWarning(null)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("sources.duplicatesDetectedTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 1 }}>
            {t("sources.duplicatesDetectedMessage")}
          </Typography>
          <List dense>
            {duplicateWarning?.duplicates.map((d) => (
              <ListItem key={d.physicalName} disablePadding>
                <ListItemText
                  primary={d.physicalName}
                  secondary={t("sources.existingAlias", { alias: d.existingAlias })}
                  primaryTypographyProps={{ variant: "body2" }}
                  secondaryTypographyProps={{ variant: "caption" }}
                />
              </ListItem>
            ))}
          </List>
          <Typography variant="body2">
            {t("sources.continueAsAliases")}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDuplicateWarning(null)}>{t("common.cancel")}</Button>
          <Button variant="contained" onClick={confirmDuplicateAdd}>
            {t("sources.addAsAliases")}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Auto-analyze prompt after Add Without Classify (Bug-389) */}
      <Dialog
        open={showAutoAnalyzePrompt}
        onClose={() => { setShowAutoAnalyzePrompt(false); setPendingAutoAnalyzeTables([]); setShowDiscover(false); }}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("sources.autoAnalyzeTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {t(
              pendingAutoAnalyzeTables.length === 1
                ? "sources.autoAnalyzeMessageSingular"
                : "sources.autoAnalyzeMessagePlural",
              { count: String(pendingAutoAnalyzeTables.length) },
            )}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => { setShowAutoAnalyzePrompt(false); setPendingAutoAnalyzeTables([]); setShowDiscover(false); }}>
            {t("common.no")}
          </Button>
          <Button
            variant="contained"
            onClick={() => autoAnalyzeMutation.mutate()}
            disabled={autoAnalyzeMutation.isPending}
          >
            {autoAnalyzeMutation.isPending ? <CircularProgress size={16} /> : t("sources.yesAnalyze")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

/* ------------------------------------------------------------------ */
/* Main SourcesPanel                                                  */
/* ------------------------------------------------------------------ */
export default function SourcesPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingSourceId, setEditingSourceId] = useState<string | null>(null);
  const [stName, setStName] = useState("");
  const [stType, setStType] = useState("postgresql");
  const [stConnId, setStConnId] = useState("");
  const [stSchema, setStSchema] = useState(defaultSchemaValue("postgresql"));
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [expandedInit, setExpandedInit] = useState(false);
  const [autoDiscoverIds, setAutoDiscoverIds] = useState<Set<string>>(new Set());
  const [calendarSourceId, setCalendarSourceId] = useState<string | null>(null);
  const [statsSourceId, setStatsSourceId] = useState<string | null>(null);
  const [aliasMapOpen, setAliasMapOpen] = useState(false);

  const sources = useSources(projectId!, modelId!);
  const connections = useConnections(projectId!);
  const focusedSourceId = useBuilderStore((s) => s.focusedSourceId);
  // Bug-5301: gate mutating controls when the model is opened read-only.
  const readOnly = useBuilderStore((s) => s.readOnly);

  useEffect(() => {
    if (expandedInit || !sources.data) return;
    const init: Record<string, boolean> = {};
    for (const s of sources.data) init[s.id] = true;
    setExpanded(init);
    setExpandedInit(true);
  }, [sources.data, expandedInit]);

  useEffect(() => {
    if (!focusedSourceId) return;
    setExpanded((prev) =>
      prev[focusedSourceId] ? prev : { ...prev, [focusedSourceId]: true },
    );
  }, [focusedSourceId]);

  const createSource = useMutation({
    mutationFn: () => {
      const data: SourceCreate = {
        project_connection_id: stConnId,
        source_type: stType,
        display_name: stName,
        default_schema: stSchema || undefined,
        config: { schema: stSchema },
      };
      return sourcesApi.create(projectId!, modelId!, data);
    },
    onSuccess: (newSource) => {
      qc.invalidateQueries({ queryKey: ["sources", projectId, modelId] });
      setDialogOpen(false);
      setExpanded((prev) => ({ ...prev, [newSource.id]: true }));
      setAutoDiscoverIds((prev) => new Set(prev).add(newSource.id));
    },
  });

  const updateSource = useMutation({
    mutationFn: () =>
      sourcesApi.update(projectId!, modelId!, editingSourceId!, {
        display_name: stName,
        project_connection_id: stConnId,
        default_schema: stSchema || undefined,
        config: { schema: stSchema },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sources", projectId, modelId] });
      setDialogOpen(false);
      setEditingSourceId(null);
    },
  });

  const deleteSource = useMutation({
    mutationFn: (sourceId: string) =>
      sourcesApi.delete(projectId!, modelId!, sourceId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["sources", projectId, modelId] }),
  });

  const confirm = useConfirm();
  async function handleDeleteSource(s: { id: string; display_name?: string }) {
    const label = s.display_name || t("sources.thisSource");
    const ok = await confirm({
      mode: "typed-name",
      title: t("sources.deleteSourceTitle"),
      message: (
        <span>
          {t("sources.deleteSourceMessage", { name: label })}
        </span>
      ),
      confirmText: label,
      confirmLabel: t("sources.deleteSourceLabel"),
    });
    if (ok) deleteSource.mutate(s.id);
  }

  function openDialog() {
    setEditingSourceId(null);
    setStName("");
    setStType("postgresql");
    setStConnId("");
    setStSchema(defaultSchemaValue("postgresql"));
    setDialogOpen(true);
  }

  function openEditSourceDialog(sourceId: string) {
    const src = sources.data?.find((s) => s.id === sourceId);
    if (!src) return;
    setEditingSourceId(sourceId);
    setStName(src.display_name);
    setStType(src.source_type);
    setStConnId(src.project_connection_id);
    setStSchema(getSourceSchema(src.config as Record<string, unknown> | undefined, src.source_type) ?? defaultSchemaValue(src.source_type));
    setDialogOpen(true);
  }

  function handleTypeChange(nextType: string) {
    const prevDefault = defaultSchemaValue(stType);
    const nextDefault = defaultSchemaValue(nextType);
    setStType(nextType);
    if (!stSchema || stSchema === prevDefault) {
      setStSchema(nextDefault);
    }
  }

  function handleConnChange(connId: string) {
    setStConnId(connId);
    const conn = connections.data?.find((c: Connection) => c.id === connId);
    if (conn) {
      // Phase C legacy fallback: the historical ``jdbc`` value collapses
      // to the canonical ``hadoop_spark`` so source forms display the
      // right options. New connections are already ``hadoop_spark``.
      const mapped =
        conn.connection_type === "jdbc" ? "hadoop_spark" : conn.connection_type;
      handleTypeChange(mapped);
    }
  }

  function toggleExpand(id: string) {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  }

  return (
    <Box>
      {/* Target section — above sources */}
      <Box mb={2}>
        <Typography variant="subtitle2" fontWeight={700} mb={1}>
          {t("sources.targetSectionTitle")}
        </Typography>
        <TargetPanel />
      </Box>

      <Divider sx={{ mb: 2 }} />

      {/* Sources header */}
      <Box display="flex" mb={1.5} gap={1}>
        <Typography variant="subtitle2" fontWeight={700} flexGrow={1}>
          {t("sources.sourcesSectionTitle")}
        </Typography>
        <Button
          size="small"
          variant="outlined"
          onClick={() => setAliasMapOpen(true)}
        >
          {t("sources.aliasMapButton")}
        </Button>
        {!readOnly && (
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={openDialog}
          >
            {t("sources.addSourceButton")}
          </Button>
        )}
      </Box>

      {sources.isLoading ? (
        <CircularProgress size={20} />
      ) : (
        <Stack spacing={1}>
          {sources.data?.map((s) => {
            const savedSchema = getSourceSchema(s.config as Record<string, unknown> | undefined, s.source_type);
            return (
            <Card key={s.id} variant="outlined">
              <CardContent sx={{ py: 1, "&:last-child": { pb: 1 } }}>
                <Box display="flex" alignItems="center" gap={1}>
                  <IconButton size="small" onClick={() => toggleExpand(s.id)}>
                    {expanded[s.id] ? (
                      <ExpandLessIcon fontSize="small" />
                    ) : (
                      <ExpandMoreIcon fontSize="small" />
                    )}
                  </IconButton>
                  <Box flexGrow={1}>
                    <Typography variant="body2" fontWeight={600}>
                      {s.display_name}
                    </Typography>
                    <Typography variant="caption" color="text.secondary" display="block" mt={0.25}>
                      {s.source_type}{savedSchema ? ` · ${savedSchema}` : ""}
                    </Typography>
                  </Box>
                  <Tooltip title={t("sources.statsTooltip")}>
                    <IconButton
                      size="small"
                      onClick={() => setStatsSourceId(s.id)}
                      data-testid={`source-stats-btn-${s.id}`}
                    >
                      <BarChartIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("sources.calendarTooltip")}>
                    <IconButton
                      size="small"
                      onClick={() => setCalendarSourceId(s.id)}
                    >
                      <CalendarMonthIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  {!readOnly && (
                    <Tooltip title={t("sources.editSourceTooltip")}>
                      <IconButton size="small" onClick={() => openEditSourceDialog(s.id)}>
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                  {!readOnly && (
                    <Tooltip title={t("sources.deleteSourceTooltip")}>
                      <IconButton size="small" onClick={() => handleDeleteSource(s)}>
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                </Box>
                <Collapse in={expanded[s.id]} unmountOnExit>
                  <Divider sx={{ my: 0.5 }} />
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    {t("sources.tablesLabel")}
                  </Typography>
                  <SourceTables
                    projectId={projectId!}
                    modelId={modelId!}
                    sourceId={s.id}
                    sourceType={s.source_type}
                    connectionId={s.project_connection_id}
                    initialSchema={String(
                      getSourceSchema(s.config as Record<string, unknown> | undefined, s.source_type) ??
                        defaultSchemaValue(s.source_type),
                    )}
                    autoDiscover={autoDiscoverIds.has(s.id)}
                  />
                </Collapse>
              </CardContent>
            </Card>
            );
          })}
          {sources.data?.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              {t("sources.noSourcesMessage")}
            </Typography>
          )}
        </Stack>
      )}

      {/* Target section moved to top */}

      {/* Create / Edit Source Dialog */}
      <Dialog open={dialogOpen} onClose={() => setDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{editingSourceId ? t("sources.editSourceDialogTitle") : t("sources.addSourceDialogTitle")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("sources.displayNameLabel")}
            fullWidth
            margin="normal"
            value={stName}
            onChange={(e) => setStName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("sources.connectionLabel")}</InputLabel>
            <Select
              value={stConnId}
              label={t("sources.connectionLabel")}
              onChange={(e) => handleConnChange(e.target.value)}
            >
              {connections.data?.map((c) => (
                <MenuItem key={c.id} value={c.id}>
                  {c.display_name} ({c.connection_type})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          {(connections.data?.length ?? 0) === 0 && (
            <Typography variant="body2" color="warning.main" mt={1}>
              {t("sources.noConnectionsWarning")}
            </Typography>
          )}
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("sources.typeLabel")}</InputLabel>
            <Select value={stType} label={t("sources.typeLabel")} onChange={(e) => handleTypeChange(e.target.value)}>
              <MenuItem value="postgresql">{t("connectionType.postgresql")}</MenuItem>
              <MenuItem value="bigquery">{t("connectionType.bigquery")}</MenuItem>
              <MenuItem value="hadoop_spark">{t("connectionType.hadoopSpark")}</MenuItem>
              <MenuItem value="redshift">{t("connectionType.redshift")}</MenuItem>
              <MenuItem value="snowflake">{t("connectionType.snowflake")}</MenuItem>
              <MenuItem value="sqlserver">{t("connectionType.sqlserver")}</MenuItem>
            </Select>
          </FormControl>
          {["redshift", "snowflake", "sqlserver"].includes(stType) && (
            <Alert severity="warning" variant="outlined" sx={{ mt: 0.5, mb: 0.5 }}>
              {t("sources.noStatisticsWarning")}
            </Alert>
          )}
          <TextField
            label={`${stType === "bigquery" ? t("sources.datasetLabel") : t("sources.schemaLabel")} *`}
            name="source-schema"
            fullWidth
            margin="normal"
            value={stSchema}
            onChange={(e) => setStSchema(e.target.value)}
            placeholder={stType === "bigquery" ? t("sources.schemaPlaceholder") : defaultSchemaValue(stType)}
            autoComplete="new-password"
          />
          <Typography variant="body2" color="text.secondary" mt={1}>
            {t("sources.afterCreatingTip")}
          </Typography>
          {(createSource.isError || updateSource.isError) && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {editingSourceId ? t("sources.updateFailed") : t("sources.createFailed")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => editingSourceId ? updateSource.mutate() : createSource.mutate()}
            disabled={!stName || !stConnId || !stSchema || createSource.isPending || updateSource.isPending}
          >
            {(createSource.isPending || updateSource.isPending)
              ? <CircularProgress size={18} />
              : editingSourceId ? t("common.save") : t("common.create")}
          </Button>
        </DialogActions>
      </Dialog>

      {calendarSourceId && (
        <CalendarTableDialog
          open={!!calendarSourceId}
          onClose={() => setCalendarSourceId(null)}
          projectId={projectId!}
          modelId={modelId!}
          sourceId={calendarSourceId}
          dialect={
            sources.data?.find((s) => s.id === calendarSourceId)?.source_type ?? "postgresql"
          }
        />
      )}

      <AliasMapDialog
        open={aliasMapOpen}
        onClose={() => setAliasMapOpen(false)}
        projectId={projectId!}
        modelId={modelId!}
      />

      <Dialog
        open={!!statsSourceId}
        onClose={() => setStatsSourceId(null)}
        maxWidth="lg"
        fullWidth
      >
        <DialogTitle>
          <Box display="flex" alignItems="center">
            <Typography variant="h6" flexGrow={1}>
              {t("sources.sourceStatisticsTitle")}
            </Typography>
            <IconButton onClick={() => setStatsSourceId(null)} size="small">
              <ClearIcon fontSize="small" />
            </IconButton>
          </Box>
        </DialogTitle>
        <DialogContent>
          {statsSourceId && <StatisticsPanel initialSourceId={statsSourceId} />}
        </DialogContent>
      </Dialog>
    </Box>
  );
}
