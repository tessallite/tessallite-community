import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Radio,
  RadioGroup,
  Select,
  Link,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import { joinsApi, tableAttributesApi } from "../../api/client";
import { useAllModelTables, useJoins, useSources } from "../../api/hooks";
import type { JoinCreate, ModelTable, TableAttribute } from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";
import { ui } from "../../theme/tokens";

/* ------------------------------------------------------------------ */
/* Join type helpers                                                    */
/* ------------------------------------------------------------------ */
function getJoinTypes(t: (key: string) => string): Array<{ value: JoinCreate["join_type"]; label: string; description: string }> {
  return [
    { value: "inner", label: t("joins.typeInner"), description: t("joins.typeInnerDesc") },
    { value: "left", label: t("joins.typeLeft"), description: t("joins.typeLeftDesc") },
    { value: "right", label: t("joins.typeRight"), description: t("joins.typeRightDesc") },
    { value: "full", label: t("joins.typeFull"), description: t("joins.typeFullDesc") },
  ];
}

/**
 * Determine which join types are allowed and what the default should be
 * when joining a fact table to a dimension table.
 *
 * Rule: outer joins should only allow NULLs on the fact side, because
 * dimension keys should always resolve. So:
 * - If left=fact, right=dim  => allow inner + left (fact side may have NULLs)
 * - If left=dim, right=fact  => allow inner + right (fact side may have NULLs)
 * - Otherwise (fact-fact, dim-dim) => allow all types
 */
function getJoinConstraints(leftTable: ModelTable | undefined, rightTable: ModelTable | undefined, t: (key: string) => string) {
  const leftIsFact = leftTable?.table_type === "fact";
  const rightIsFact = rightTable?.table_type === "fact";
  const leftIsDim = leftTable?.table_type?.startsWith("dim") ?? false;
  const rightIsDim = rightTable?.table_type?.startsWith("dim") ?? false;

  if (leftIsFact && rightIsDim) {
    return {
      allowed: new Set<string>(["inner", "left"]),
      hint: t("joins.hintLeftOuter"),
    };
  }
  if (leftIsDim && rightIsFact) {
    return {
      allowed: new Set<string>(["inner", "right"]),
      hint: t("joins.hintRightOuter"),
    };
  }
  return { allowed: new Set<string>(["inner", "left", "right", "full"]), hint: null };
}

/* ------------------------------------------------------------------ */
/* Column selector — reads synced table attributes from model metadata */
/* ------------------------------------------------------------------ */
function useTableColumns(projectId: string, modelId: string, table: ModelTable | undefined) {
  return useQuery({
    queryKey: ["tableAttributes", projectId, modelId, table?.id],
    queryFn: () => tableAttributesApi.list(projectId, modelId, table!.id),
    enabled: !!projectId && !!modelId && !!table?.id,
    staleTime: 20 * 1000,
  });
}

/* ------------------------------------------------------------------ */
/* Main JoinsPanel                                                     */
/* ------------------------------------------------------------------ */
export default function JoinsPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingJoinId, setEditingJoinId] = useState<string | null>(null);
  const [leftTableId, setLeftTableId] = useState("");
  const [rightTableId, setRightTableId] = useState("");
  const [joinType, setJoinType] = useState<JoinCreate["join_type"]>("inner");
  const [leftCol, setLeftCol] = useState("");
  const [rightCol, setRightCol] = useState("");

  const pendingJoin        = useBuilderStore((s) => s.pendingJoin);
  const setPendingJoin     = useBuilderStore((s) => s.setPendingJoin);
  const selectedObjectId   = useBuilderStore((s) => s.selectedObjectId);
  const selectedObjectType = useBuilderStore((s) => s.selectedObjectType);
  const selectObject       = useBuilderStore((s) => s.selectObject);

  // The join that was clicked on the canvas (if any)
  const focusedJoinId =
    selectedObjectType === "join" ? selectedObjectId : null;

  // Scroll the focused join card into view
  const joinRefs = useRef<Record<string, HTMLDivElement | null>>({});
  useEffect(() => {
    if (!focusedJoinId) return;
    const el = joinRefs.current[focusedJoinId];
    el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [focusedJoinId]);

  const sources = useSources(projectId!, modelId!);
  const sourceIds = (sources.data ?? []).map((s) => s.id);
  const allTables = useAllModelTables(projectId!, modelId!, sourceIds);
  const joins = useJoins(projectId!, modelId!);

  // Resolve selected tables
  const leftTable = allTables.data?.find((t) => t.id === leftTableId);
  const rightTable = allTables.data?.find((t) => t.id === rightTableId);

  // Fetch columns for selected tables from synced table attributes.
  // Using table attributes (not discoverColumns) so calendar tables and
  // aggregate-schema tables always resolve — their attributes are synced
  // at creation time and don't require a live source connection query.
  const leftColumns  = useTableColumns(projectId!, modelId!, leftTable);
  const rightColumns = useTableColumns(projectId!, modelId!, rightTable);

  // Join type constraints based on fact/dim relationship
  const constraints = getJoinConstraints(leftTable, rightTable, t);
  const JOIN_TYPES = getJoinTypes(t);

  // When constraints change, reset join type if current is no longer allowed
  useEffect(() => {
    if (!constraints.allowed.has(joinType)) {
      setJoinType("inner");
    }
  }, [leftTableId, rightTableId]);

  // Handle pending join from canvas drag
  useEffect(() => {
    if (pendingJoin) {
      setLeftTableId(pendingJoin.leftTableId);
      setRightTableId(pendingJoin.rightTableId);
      setJoinType("inner");
      setLeftCol("");
      setRightCol("");
      setDialogOpen(true);
      setPendingJoin(null);
    }
  }, [pendingJoin, setPendingJoin]);

  const createJoin = useMutation({
    mutationFn: () =>
      joinsApi.create(projectId!, modelId!, {
        left_table_id: leftTableId,
        right_table_id: rightTableId,
        join_type: joinType,
        left_column_name: leftCol,
        right_column_name: rightCol,
      }),
    onSuccess: (created) => {
      window.dispatchEvent(
        new CustomEvent("canvas-history-action", {
          detail: {
            action: {
              type: "addLink",
              joinId: created.id,
              createData: {
                left_table_id: created.left_table_id,
                right_table_id: created.right_table_id,
                join_type: created.join_type,
                left_column_name: created.left_column_name ?? leftCol,
                right_column_name: created.right_column_name ?? rightCol,
              },
            },
          },
        }),
      );
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      setDialogOpen(false);
    },
  });

  const updateJoin = useMutation({
    mutationFn: () =>
      joinsApi.update(projectId!, modelId!, editingJoinId!, {
        join_type: joinType,
        left_column_name: leftCol,
        right_column_name: rightCol,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      setDialogOpen(false);
      setEditingJoinId(null);
    },
  });

  const deleteJoin = useMutation({
    mutationFn: (joinId: string) =>
      joinsApi.delete(projectId!, modelId!, joinId),
    onSuccess: (_data, joinId) => {
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
    },
  });

  const relationTerminalOverrides = useBuilderStore((s) => s.relationTerminalOverrides);
  const setRelationTerminalOverride = useBuilderStore((s) => s.setRelationTerminalOverride);

  function handleToggleTerminal(j: any, side: "source" | "target") {
    const leftIsFact = allTables.data?.find((t) => t.id === j.left_table_id)?.table_type === "fact";
    const rightIsFact = allTables.data?.find((t) => t.id === j.right_table_id)?.table_type === "fact";
    const currentOverride = relationTerminalOverrides[j.id] || {
      source: leftIsFact && !rightIsFact ? "many" : !leftIsFact && rightIsFact ? "one" : "none",
      target: leftIsFact && !rightIsFact ? "one" : !leftIsFact && rightIsFact ? "many" : "none",
    };
    const current = currentOverride[side];
    const next = current === "many" ? "one" : current === "one" ? "optional" : "many";
    setRelationTerminalOverride(j.id, { ...currentOverride, [side]: next });
  }

  const confirm = useConfirm();
  async function handleDeleteJoin(joinId: string) {
    const ok = await confirm({
      title: t("joins.deleteConfirm"),
      message: t("joins.deleteMessage"),
      confirmLabel: t("joins.delete"),
    });
    if (ok) {
      const j = joins.data?.find((j) => j.id === joinId);
      if (j) {
        window.dispatchEvent(
          new CustomEvent("canvas-history-action", {
            detail: {
              action: {
                type: "deleteLink",
                joinId,
                createData: {
                  left_table_id: j.left_table_id,
                  right_table_id: j.right_table_id,
                  join_type: j.join_type as JoinCreate["join_type"],
                  left_column_name: j.left_column_name ?? "",
                  right_column_name: j.right_column_name ?? "",
                },
              },
            },
          }),
        );
      }
      deleteJoin.mutate(joinId);
      selectObject(null, null);
    }
  }

  function tableLabel(id: string) {
    const tbl = allTables.data?.find((tbl) => tbl.id === id);
    return tbl?.alias ?? tbl?.display_name ?? id.slice(0, 8);
  }

  function openDialog() {
    setEditingJoinId(null);
    setLeftTableId("");
    setRightTableId("");
    setJoinType("inner");
    setLeftCol("");
    setRightCol("");
    setDialogOpen(true);
  }

  function openEditDialog(joinId: string) {
    const j = joins.data?.find((j) => j.id === joinId);
    if (!j) return;
    setEditingJoinId(joinId);
    setLeftTableId(j.left_table_id);
    setRightTableId(j.right_table_id);
    setJoinType(j.join_type as JoinCreate["join_type"]);
    setLeftCol(j.left_column_name ?? "");
    setRightCol(j.right_column_name ?? "");
    setDialogOpen(true);
  }

  const isConnectingMode  = useBuilderStore((s) => s.isConnectingMode);
  const setConnectingMode = useBuilderStore((s) => s.setConnectingMode);
  const closePanel        = useBuilderStore((s) => s.closePanel);

  return (
    <Box>
      {/* Connection drawing mode banner */}
      {isConnectingMode ? (
        <Alert
          severity="info"
          sx={{ mb: 1.5, alignItems: "flex-start" }}
          action={
            <Button
              size="small"
              color="inherit"
              onClick={() => { setConnectingMode(false); closePanel(); }}
              sx={{ whiteSpace: "nowrap", fontWeight: 600 }}
            >
              {t("joins.endConnectionDrawing")}
            </Button>
          }
        >
          <Typography variant="body2" fontWeight={600} mb={0.25}>
            {t("joins.connectionModeActive")}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t("joins.connectionModeHelp")}
          </Typography>
        </Alert>
      ) : (
        <Alert severity="info" sx={{ mb: 1.5 }}>
          <Typography variant="caption" color="text.secondary">
            {t("joins.canvasHelp")}
          </Typography>
        </Alert>
      )}

      <Box display="flex" alignItems="center" mb={1.5}>
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
          {t("joins.description")}{" "}
          <Link
            href="/help/modelling/dimension-aliases.html"
            target="_blank"
            rel="noopener"
          >
            {t("common.learnMore")}
          </Link>
          .
        </Typography>
        <Button
          size="small"
          variant="contained"
          startIcon={<AddIcon />}
          onClick={openDialog}
          sx={{ ml: 1, whiteSpace: "nowrap" }}
        >
          {t("joins.add")}
        </Button>
      </Box>

      {joins.isLoading ? (
        <CircularProgress size={20} />
      ) : joins.data?.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("joins.none")}
        </Typography>
      ) : (
        <Stack spacing={1}>
          {joins.data?.map((j) => {
            const isFocused = j.id === focusedJoinId;
            const leftIsFact = allTables.data?.find((t) => t.id === j.left_table_id)?.table_type === "fact";
            const rightIsFact = allTables.data?.find((t) => t.id === j.right_table_id)?.table_type === "fact";
            const override = relationTerminalOverrides[j.id];
            const srcMarker = override?.source || (leftIsFact && !rightIsFact ? "many" : !leftIsFact && rightIsFact ? "one" : "none");
            const tgtMarker = override?.target || (leftIsFact && !rightIsFact ? "one" : !leftIsFact && rightIsFact ? "many" : "none");

            return (
              <Card
                key={j.id}
                variant="outlined"
                ref={(el) => { joinRefs.current[j.id] = el; }}
                onClick={() => selectObject(j.id, "join")}
                sx={{
                  cursor: "pointer",
                  borderColor: isFocused ? "primary.main" : "divider",
                  borderWidth: isFocused ? 2 : 1,
                  transition: "border-color 0.15s",
                  "&:hover": { borderColor: "primary.main" },
                }}
              >
                <CardContent sx={{ py: 1, px: 1.5, "&:last-child": { pb: 1 } }}>
                  {/* Row 1 — table names + join type */}
                  <Box display="flex" alignItems="center" gap={0.5} mb={0.5} minWidth={0}>
                    <Tooltip title={t("joins.cycleCardinality")} arrow placement="top">
                      <Button
                        size="small"
                        variant="outlined"
                        onClick={(e) => { e.stopPropagation(); handleToggleTerminal(j, "source"); }}
                        sx={{
                          minWidth: 0,
                          px: 0.75,
                          py: 0,
                          fontSize: 11,
                          fontWeight: 600,
                          lineHeight: "22px",
                          textTransform: "none",
                          borderColor: "divider",
                          color: "text.primary",
                          "&:hover": { borderColor: "primary.main", color: "primary.main" },
                        }}
                      >
                        {srcMarker}
                      </Button>
                    </Tooltip>
                    <Typography variant="body2" fontWeight={600} noWrap sx={{ flex: "1 1 0", minWidth: 0 }}>
                      {tableLabel(j.left_table_id)}
                    </Typography>
                    <Typography
                      variant="caption"
                      sx={{ flexShrink: 0, bgcolor: ui.greenBg, color: ui.green, fontWeight: 600, px: 1, py: 0.25, borderRadius: 1 }}
                    >
                      {j.join_type.toUpperCase()}
                    </Typography>
                    <Typography variant="body2" fontWeight={600} noWrap sx={{ flex: "1 1 0", minWidth: 0, textAlign: "right" }}>
                      {tableLabel(j.right_table_id)}
                    </Typography>
                    <Tooltip title={t("joins.cycleCardinality")} arrow placement="top">
                      <Button
                        size="small"
                        variant="outlined"
                        onClick={(e) => { e.stopPropagation(); handleToggleTerminal(j, "target"); }}
                        sx={{
                          minWidth: 0,
                          px: 0.75,
                          py: 0,
                          fontSize: 11,
                          fontWeight: 600,
                          lineHeight: "22px",
                          textTransform: "none",
                          borderColor: "divider",
                          color: "text.primary",
                          "&:hover": { borderColor: "primary.main", color: "primary.main" },
                        }}
                      >
                        {tgtMarker}
                      </Button>
                    </Tooltip>
                  </Box>
                  {/* Row 2 — column keys + action buttons */}
                  <Box display="flex" alignItems="center" gap={0.5}>
                    <Typography variant="caption" color="text.secondary" noWrap sx={{ flex: 1, minWidth: 0 }}>
                      {j.left_column_name ?? t("joins.unknownColumn")} = {j.right_column_name ?? t("joins.unknownColumn")}
                    </Typography>
                    <Tooltip title={t("joins.resetPath")}>
                      <IconButton size="small" onClick={(e) => {
                        e.stopPropagation();
                        window.dispatchEvent(new CustomEvent('reset-edge-path', { detail: j.id }));
                      }}>
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("joins.togglePathing")}>
                      <IconButton size="small" onClick={(e) => {
                        e.stopPropagation();
                        window.dispatchEvent(new CustomEvent('toggle-edge-pathing-auto', { detail: j.id }));
                      }}>
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("joins.edit")}>
                      <IconButton size="small" onClick={(e) => { e.stopPropagation(); openEditDialog(j.id); }}>
                        <EditIcon sx={{ fontSize: 15 }} />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("joins.delete")}>
                      <IconButton
                        size="small"
                        onClick={(e) => { e.stopPropagation(); handleDeleteJoin(j.id); }}
                      >
                        <DeleteIcon sx={{ fontSize: 15 }} />
                      </IconButton>
                    </Tooltip>
                  </Box>
                </CardContent>
              </Card>
            );
          })}
        </Stack>
      )}

      {/* Join creation dialog */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{editingJoinId ? t("joins.editTitle") : t("joins.addTitle")}</DialogTitle>
        <DialogContent>
          {/* Left table */}
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("joins.leftTable")}</InputLabel>
            <Select
              value={leftTableId}
              label={t("joins.leftTable")}
              disabled={!!editingJoinId}
              onChange={(e) => {
                setLeftTableId(e.target.value);
                setLeftCol("");
              }}
            >
              {allTables.data?.map((tbl) => (
                <MenuItem key={tbl.id} value={tbl.id}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{tbl.alias ?? tbl.display_name}</span>
                    <Chip
                      label={tbl.table_type === "fact" ? t("joins.tableFact") : tbl.table_type === "dim_detail" ? t("joins.tableDimDetail") : t("joins.tableDimAgg")}
                      size="small"
                      sx={{
                        height: 18, fontSize: 10, fontWeight: 500,
                        bgcolor: tbl.table_type === "fact" ? ui.greenBg : tbl.table_type === "dim_detail" ? ui.goldBg : ui.purpleBg,
                        color: tbl.table_type === "fact" ? ui.green : tbl.table_type === "dim_detail" ? ui.goldDark : ui.purple,
                      }}
                    />
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Left column */}
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("joins.leftColumn")}</InputLabel>
            <Select
              value={leftCol}
              label={t("joins.leftColumn")}
              disabled={!leftTableId || leftColumns.isLoading}
              onChange={(e) => setLeftCol(e.target.value)}
            >
              {leftColumns.isLoading && (
                <MenuItem disabled>
                  <CircularProgress size={14} sx={{ mr: 1 }} /> {t("joins.loadingColumns")}
                </MenuItem>
              )}
              {leftColumns.data?.map((c: TableAttribute) => (
                <MenuItem key={c.name} value={c.name}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{c.name}</span>
                    <Typography variant="caption" color="text.secondary">
                      {c.data_type}
                    </Typography>
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Join type radio group */}
          <Box sx={{ mt: 2, mb: 1, p: 1.5, bgcolor: ui.mutedBg, borderRadius: 1 }}>
            <Typography variant="subtitle2" fontWeight={700} mb={0.5}>
              {t("joins.joinType")}
            </Typography>
            {constraints.hint && (
              <Alert severity="info" sx={{ mb: 1, py: 0 }}>
                <Typography variant="caption">{constraints.hint}</Typography>
              </Alert>
            )}
            <RadioGroup
              value={joinType}
              onChange={(e) => setJoinType(e.target.value as JoinCreate["join_type"])}
            >
              {JOIN_TYPES.map((jt) => {
                const disabled = !constraints.allowed.has(jt.value);
                return (
                  <FormControlLabel
                    key={jt.value}
                    value={jt.value}
                    disabled={disabled}
                    control={<Radio size="small" />}
                    label={
                      <Box>
                        <Typography variant="body2" fontWeight={joinType === jt.value ? 700 : 400}>
                          {jt.label}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {jt.description}
                        </Typography>
                      </Box>
                    }
                    sx={{ mb: 0.5 }}
                  />
                );
              })}
            </RadioGroup>
          </Box>

          {/* Right table */}
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("joins.rightTable")}</InputLabel>
            <Select
              value={rightTableId}
              label={t("joins.rightTable")}
              disabled={!!editingJoinId}
              onChange={(e) => {
                setRightTableId(e.target.value);
                setRightCol("");
              }}
            >
              {allTables.data?.map((tbl) => (
                <MenuItem key={tbl.id} value={tbl.id}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{tbl.alias ?? tbl.display_name}</span>
                    <Chip
                      label={tbl.table_type === "fact" ? t("joins.tableFact") : tbl.table_type === "dim_detail" ? t("joins.tableDimDetail") : t("joins.tableDimAgg")}
                      size="small"
                      sx={{
                        height: 18, fontSize: 10, fontWeight: 500,
                        bgcolor: tbl.table_type === "fact" ? ui.greenBg : tbl.table_type === "dim_detail" ? ui.goldBg : ui.purpleBg,
                        color: tbl.table_type === "fact" ? ui.green : tbl.table_type === "dim_detail" ? ui.goldDark : ui.purple,
                      }}
                    />
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Right column */}
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("joins.rightColumn")}</InputLabel>
            <Select
              value={rightCol}
              label={t("joins.rightColumn")}
              disabled={!rightTableId || rightColumns.isLoading}
              onChange={(e) => setRightCol(e.target.value)}
            >
              {rightColumns.isLoading && (
                <MenuItem disabled>
                  <CircularProgress size={14} sx={{ mr: 1 }} /> {t("joins.loadingColumns")}
                </MenuItem>
              )}
              {rightColumns.data?.map((c: TableAttribute) => (
                <MenuItem key={c.name} value={c.name}>
                  <Box display="flex" alignItems="center" gap={1}>
                    <span>{c.name}</span>
                    <Typography variant="caption" color="text.secondary">
                      {c.data_type}
                    </Typography>
                  </Box>
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {(createJoin.isError || updateJoin.isError) && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {editingJoinId ? t("joins.failedToUpdate") : t("joins.failedToCreate")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => editingJoinId ? updateJoin.mutate() : createJoin.mutate()}
            disabled={
              !leftTableId ||
              !rightTableId ||
              !leftCol ||
              !rightCol ||
              createJoin.isPending ||
              updateJoin.isPending
            }
          >
            {(createJoin.isPending || updateJoin.isPending)
              ? <CircularProgress size={18} />
              : editingJoinId ? t("common.save") : t("joins.add")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
