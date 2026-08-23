import { useMemo, useState, useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  IconButton,
  List,
  ListItem,
  ListItemIcon,
  ListItemText,
  Paper,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import { useT } from "../../i18n";
import { attributeRelationshipsApi } from "../../api/client";
import {
  useAttributeRelationships,
  useTableAttributes,
  useJoins,
} from "../../api/hooks";
import { useConfirm } from "../Confirm";
import { recordCreate, recordDelete, recordUpdate } from "../Builder/emitDrawerHistory";
import { ui, statusColor, type StatusSeverity } from "../../theme/tokens";
import type { AttributeRelationshipStatus, DimensionAttributeRelationship } from "../../api/types";

/** Advisory validation result for one column, from the validate endpoint. */
interface AdvisoryResult {
  column: string;
  table_id?: string | null;
  is_bijection: boolean;
  reason: string;
  error?: string | null;
}

/** A candidate column from either the dimension or fact table. */
interface CandidateColumn {
  id: string;
  name: string;
  tableId: string;
  tableLabel: string;
  is_user_defined: boolean;
}

/** Map a verification status onto the shared status-chip severity palette. */
function statusSeverity(status: AttributeRelationshipStatus): StatusSeverity {
  switch (status) {
    case "VERIFIED":
      return "success";
    case "BROKEN":
    case "ERROR":
      return "error";
    case "STALE":
      return "stale";
    case "PENDING":
      // Bug-7894: a text 1:1 detail awaiting artifact-build collation
      // certification — in progress, not an error and not yet serving.
      return "creating";
    default:
      return "default";
  }
}

/** Map an advisory reason to an i18n key suffix. */
function advisoryLabelKey(reason: string): string {
  switch (reason) {
    case "ok":
      return "attributeRelationships.advisoryOk";
    case "forward_violation":
    case "reverse_violation":
    case "null_endpoint":
      return "attributeRelationships.advisoryNotBijection";
    case "error":
      return "attributeRelationships.advisoryError";
    default:
      return "attributeRelationships.advisoryNotChecked";
  }
}

function advisorySeverity(reason: string): StatusSeverity {
  switch (reason) {
    case "ok":
      return "success";
    case "forward_violation":
    case "reverse_violation":
    case "null_endpoint":
      return "error";
    case "error":
      return "stale";
    default:
      return "default";
  }
}

function relationshipToPayload(
  relationship: DimensionAttributeRelationship,
  dimensionId: string,
): Record<string, unknown> {
  return {
    detail_column_name: relationship.detail_column_name,
    cardinality: relationship.cardinality,
    key_column_name: relationship.key_column_name,
    enabled: relationship.enabled,
    __dimension_id: dimensionId,
  };
}

interface Props {
  projectId: string;
  modelId: string;
  dimensionId: string;
  /** ModelTable that owns the dimension's key column. */
  sourceTableId: string | null;
  /** The dimension's key column name, excluded from the detail-column picker. */
  keyColumnName: string | null;
  canEdit: boolean;
}

/**
 * Declare / manage dimension attribute relationships (derived-grain routing).
 *
 * Multi-select bulk add of bijection detail columns from BOTH the dimension
 * table and the fact table (via the Join). Optional advisory "Validate" pre-check.
 * Delete shows downstream usage and an aggregate retirement prompt.
 */
export default function AttributeRelationshipsSection({
  projectId,
  modelId,
  dimensionId,
  sourceTableId,
  keyColumnName,
  canEdit,
}: Props) {
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();

  const rels = useAttributeRelationships(projectId, modelId, dimensionId);
  const dimTableAttrs = useTableAttributes(
    projectId,
    modelId,
    sourceTableId ?? "",
  );
  const joins = useJoins(projectId, modelId);

  // Resolve the fact table joined to this dimension's source table.
  const factTableId = useMemo(() => {
    if (!sourceTableId || !joins.data) return null;
    for (const j of joins.data) {
      if (j.left_table_id === sourceTableId) return j.right_table_id;
      if (j.right_table_id === sourceTableId) return j.left_table_id;
    }
    return null;
  }, [sourceTableId, joins.data]);

  const factTableAttrs = useTableAttributes(
    projectId,
    modelId,
    factTableId ?? "",
  );

  // Find the FK column names from the join (to exclude from candidates).
  const joinColumnNames = useMemo(() => {
    const names = new Set<string>();
    if (!sourceTableId || !joins.data) return names;
    for (const j of joins.data) {
      if (j.left_table_id === sourceTableId || j.right_table_id === sourceTableId) {
        if (j.left_column_name) names.add(j.left_column_name);
        if (j.right_column_name) names.add(j.right_column_name);
      }
    }
    return names;
  }, [sourceTableId, joins.data]);

  const [showForm, setShowForm] = useState(false);
  // Multi-select: set of "tableId:columnName" keys.
  const [selectedColumns, setSelectedColumns] = useState<Set<string>>(new Set());
  const [advisoryResults, setAdvisoryResults] = useState<
    Record<string, AdvisoryResult>
  >({});
  const [validating, setValidating] = useState(false);

  const declaredDetailNames = useMemo(
    () => new Set((rels.data ?? []).map((r) => r.detail_column_name).filter(Boolean)),
    [rels.data],
  );

  // Build the candidate list from both tables.
  const detailCandidates: CandidateColumn[] = useMemo(() => {
    const candidates: CandidateColumn[] = [];

    // Dimension table columns.
    for (const a of dimTableAttrs.data ?? []) {
      if (
        a.is_user_defined ||
        a.name === keyColumnName ||
        declaredDetailNames.has(a.name) ||
        joinColumnNames.has(a.name)
      ) {
        continue;
      }
      candidates.push({
        id: a.id,
        name: a.name,
        tableId: sourceTableId!,
        tableLabel: t("attributeRelationships.dimTableLabel"),
        is_user_defined: a.is_user_defined,
      });
    }

    // Fact table columns.
    if (factTableId) {
      for (const a of factTableAttrs.data ?? []) {
        if (
          a.is_user_defined ||
          declaredDetailNames.has(a.name) ||
          joinColumnNames.has(a.name)
        ) {
          continue;
        }
        candidates.push({
          id: a.id,
          name: a.name,
          tableId: factTableId,
          tableLabel: t("attributeRelationships.factTableLabel"),
          is_user_defined: a.is_user_defined,
        });
      }
    }

    return candidates;
  }, [
    dimTableAttrs.data,
    factTableAttrs.data,
    keyColumnName,
    declaredDetailNames,
    joinColumnNames,
    sourceTableId,
    factTableId,
    t,
  ]);

  function candidateKey(c: CandidateColumn) {
    return `${c.tableId}:${c.name}`;
  }

  function invalidate() {
    qc.invalidateQueries({
      queryKey: ["attributeRelationships", projectId, modelId, dimensionId],
    });
    qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
  }

  function resetForm() {
    setShowForm(false);
    setSelectedColumns(new Set());
    setAdvisoryResults({});
  }

  const toggleColumn = useCallback((key: string) => {
    setSelectedColumns((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }, []);

  // Bulk create: loop single creates for each selected column.
  const bulkCreate = useMutation({
    mutationFn: async () => {
      const created: Array<{ id: string; payload: Record<string, unknown> }> = [];
      const entries = Array.from(selectedColumns).map((key) => {
        const [, ...rest] = key.split(":");
        return rest.join(":");
      });
      for (const colName of entries) {
        const payload = {
          detail_column_name: colName,
          cardinality: "BIJECTION",
          enabled: true,
        } as const;
        const result = await attributeRelationshipsApi.create(projectId, modelId, dimensionId, payload);
        created.push({
          id: result.id,
          payload: { ...payload, __dimension_id: dimensionId },
        });
      }
      return created;
    },
    onSuccess: (created) => {
      for (const item of created) {
        recordCreate("attributeRelationship", item.id, item.payload);
      }
      invalidate();
      resetForm();
    },
  });

  // Advisory validate.
  async function handleValidate() {
    if (selectedColumns.size === 0) return;
    setValidating(true);
    try {
      const specs = Array.from(selectedColumns).map((key) => {
        const idx = key.indexOf(":");
        return {
          name: key.substring(idx + 1),
          table_id: key.substring(0, idx),
        };
      });
      const results = await attributeRelationshipsApi.validate(
        projectId,
        modelId,
        dimensionId,
        specs,
      );
      const map: Record<string, AdvisoryResult> = {};
      for (const r of results) {
        const k = r.table_id ? `${r.table_id}:${r.column}` : r.column;
        map[k] = r;
      }
      setAdvisoryResults(map);
    } catch {
      // Swallow; the UI stays at "Not checked".
    } finally {
      setValidating(false);
    }
  }

  const toggleRel = useMutation({
    mutationFn: (vars: { relId: string; enabled: boolean; prior: Record<string, unknown> }) =>
      attributeRelationshipsApi.update(
        projectId,
        modelId,
        dimensionId,
        vars.relId,
        { enabled: vars.enabled },
      ),
    onSuccess: (_updated, variables) => {
      recordUpdate(
        "attributeRelationship",
        variables.relId,
        variables.prior,
        { enabled: variables.enabled, __dimension_id: dimensionId },
      );
      invalidate();
    },
  });

  const deleteRel = useMutation({
    mutationFn: (vars: { relId: string; retireAggregates: boolean; prior: Record<string, unknown> }) =>
      attributeRelationshipsApi.delete(
        projectId,
        modelId,
        dimensionId,
        vars.relId,
        vars.retireAggregates,
      ),
    onSuccess: (_deleted, variables) => {
      recordDelete("attributeRelationship", variables.relId, {
        ...variables.prior,
        __dimension_id: dimensionId,
        __retire_aggregates: variables.retireAggregates,
      });
      invalidate();
    },
  });

  function toggleCreateForm() {
    bulkCreate.reset();
    setSelectedColumns(new Set());
    setAdvisoryResults({});
    setShowForm((v) => !v);
  }

  async function handleDelete(relId: string, detailName: string | null) {
    let usageMsg = "";
    let hasAggregates = false;
    try {
      const usage = await attributeRelationshipsApi.downstreamUsage(
        projectId,
        modelId,
        dimensionId,
        relId,
      );
      const parts: string[] = [];
      if (usage.linked_dimensions.length > 0) {
        parts.push(
          t("attributeRelationships.deleteLinkedDims", {
            count: usage.linked_dimensions.length,
            names: usage.linked_dimensions.map((d) => d.name).join(", "),
          }),
        );
      }
      if (usage.affected_aggregates.length > 0) {
        hasAggregates = true;
        parts.push(
          t("attributeRelationships.deleteAffectedAggs", {
            count: usage.affected_aggregates.length,
            names: usage.affected_aggregates
              .map((a) => a.physical_table_name)
              .join(", "),
          }),
        );
      }
      if (parts.length > 0) {
        usageMsg = "\n\n" + parts.join("\n");
      }
    } catch {
      // Continue without usage info.
    }

    const confirmMsg =
      t("attributeRelationships.deleteMessage", {
        detail: detailName ?? "",
      }) + usageMsg;

    // First: confirm the delete itself (user can abort here).
    const deleteOk = await confirm({
      title: t("attributeRelationships.deleteTitle"),
      message: (
        <span style={{ whiteSpace: "pre-line" }}>{confirmMsg}</span>
      ),
      confirmLabel: t("common.delete"),
    });
    if (!deleteOk) return;

    const prior = (rels.data ?? []).find((relationship) => relationship.id === relId);
    if (!prior) return;
    const priorPayload = relationshipToPayload(prior, dimensionId);

    if (hasAggregates) {
      // Second: ask about aggregate retirement (after confirming delete).
      const retireOk = await confirm({
        title: t("attributeRelationships.retireAggregatesPrompt"),
        message: (
          <span>{t("attributeRelationships.retireAggregatesPrompt")}</span>
        ),
        confirmLabel: t("attributeRelationships.retireAndDelete"),
        cancelLabel: t("attributeRelationships.deleteOnly"),
      });
      deleteRel.mutate({ relId, retireAggregates: !!retireOk, prior: priorPayload });
    } else {
      deleteRel.mutate({ relId, retireAggregates: false, prior: priorPayload });
    }
  }

  const showTable = !!sourceTableId && !rels.isError;

  return (
    <Box sx={{ mt: 2 }}>
      <Box display="flex" alignItems="center" mb={0.5}>
        <Typography variant="subtitle2" sx={{ flex: 1 }}>
          {t("attributeRelationships.title")}
        </Typography>
        {canEdit && sourceTableId && (
          <Button
            size="small"
            startIcon={<AddIcon />}
            onClick={toggleCreateForm}
            sx={{ whiteSpace: "nowrap" }}
          >
            {t("attributeRelationships.add")}
          </Button>
        )}
      </Box>
      <Typography
        variant="caption"
        color="text.secondary"
        sx={{ display: "block", mb: 1 }}
      >
        {t("attributeRelationships.help")}
      </Typography>

      {/* Key column display. */}
      {keyColumnName && (
        <Box display="flex" alignItems="center" gap={0.75} mb={1}>
          <Typography variant="caption" color="text.secondary">
            {t("attributeRelationships.keyColumnLabel")}:
          </Typography>
          <Typography
            variant="caption"
            sx={{ fontFamily: "monospace", fontSize: 12, fontWeight: 500 }}
          >
            {keyColumnName}
          </Typography>
        </Box>
      )}

      {!sourceTableId && (
        <Typography variant="caption" color="text.secondary">
          {t("attributeRelationships.needsPhysicalKey")}
        </Typography>
      )}

      {sourceTableId && rels.isError && (
        <Alert severity="error" sx={{ mb: 1 }}>
          {t("attributeRelationships.loadFailed")}
        </Alert>
      )}

      {(toggleRel.isError || deleteRel.isError) && (
        <Alert
          severity="error"
          sx={{ mb: 1 }}
          onClose={() => {
            toggleRel.reset();
            deleteRel.reset();
          }}
        >
          {t("attributeRelationships.updateFailed")}
        </Alert>
      )}

      {sourceTableId && rels.isLoading ? (
        <CircularProgress size={18} />
      ) : showTable ? (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                <TableCell>
                  <strong>{t("attributeRelationships.mapping")}</strong>
                </TableCell>
                <TableCell>
                  <strong>{t("attributeRelationships.status")}</strong>
                </TableCell>
                <TableCell>
                  <strong>{t("attributeRelationships.enabled")}</strong>
                </TableCell>
                {canEdit && <TableCell />}
              </TableRow>
            </TableHead>
            <TableBody>
              {rels.data?.map((r) => {
                const sev = statusSeverity(r.verification_status);
                const { bg, fg } = statusColor(sev);
                return (
                  <TableRow key={r.id}>
                    <TableCell>
                      {keyColumnName && (
                        <Typography
                          component="span"
                          variant="body2"
                          sx={{
                            fontFamily: "monospace",
                            fontSize: 12,
                            color: "text.secondary",
                          }}
                        >
                          {keyColumnName} {"-> "}
                        </Typography>
                      )}
                      <Typography
                        component="span"
                        variant="body2"
                        sx={{ fontFamily: "monospace", fontSize: 12 }}
                      >
                        {r.detail_column_name ?? "--"}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Tooltip
                        title={t(
                          `attributeRelationships.statusHelp.${r.verification_status}`,
                        )}
                      >
                        <Typography
                          component="span"
                          variant="caption"
                          sx={{
                            px: 0.75,
                            py: 0.25,
                            borderRadius: 0.5,
                            bgcolor: bg,
                            color: fg,
                            fontWeight: 500,
                            fontSize: 11,
                          }}
                        >
                          {t(
                            `attributeRelationships.statusLabel.${r.verification_status}`,
                          )}
                        </Typography>
                      </Tooltip>
                    </TableCell>
                    <TableCell>
                      <Switch
                        size="small"
                        checked={r.enabled}
                        disabled={!canEdit || toggleRel.isPending}
                        onChange={(e) =>
                          toggleRel.mutate({
                            relId: r.id,
                            enabled: e.target.checked,
                            prior: relationshipToPayload(r, dimensionId),
                          })
                        }
                      />
                    </TableCell>
                    {canEdit && (
                      <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                        <Tooltip title={t("common.delete")}>
                          <IconButton
                            size="small"
                            onClick={() =>
                              handleDelete(r.id, r.detail_column_name)
                            }
                          >
                            <DeleteIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
              {rels.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={canEdit ? 4 : 3}>
                    <Typography variant="caption" color="text.secondary">
                      {t("attributeRelationships.none")}
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      ) : null}

      {/* Multi-select add form */}
      {canEdit && sourceTableId && showForm && (
        <Paper variant="outlined" sx={{ p: 1.5, mt: 1 }}>
          {dimTableAttrs.isError && (
            <Alert severity="error" sx={{ mb: 1 }}>
              {t("attributeRelationships.columnsFailed")}
            </Alert>
          )}

          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ display: "block", mb: 0.5 }}
          >
            {t("attributeRelationships.multiSelectHelp")}
          </Typography>

          {dimTableAttrs.isLoading ? (
            <CircularProgress size={18} />
          ) : detailCandidates.length === 0 ? (
            <Typography variant="caption" color="text.secondary">
              {t("attributeRelationships.noCandidates")}
            </Typography>
          ) : (
            <List dense sx={{ maxHeight: 240, overflow: "auto", mb: 1 }}>
              {detailCandidates.map((c) => {
                const key = candidateKey(c);
                const checked = selectedColumns.has(key);
                const advisory = advisoryResults[key];
                const isDimTable = c.tableId === sourceTableId;
                return (
                  <ListItem
                    key={key}
                    dense
                    sx={{ py: 0 }}
                    secondaryAction={
                      <Box display="flex" alignItems="center" gap={0.5}>
                        {!isDimTable && (
                          <Chip
                            size="small"
                            label={t("attributeRelationships.factSideNote")}
                            variant="outlined"
                            sx={{ fontSize: 10, height: 20 }}
                          />
                        )}
                        {advisory ? (
                          <Chip
                            size="small"
                            label={t(advisoryLabelKey(advisory.reason))}
                            sx={{
                              bgcolor: statusColor(
                                advisorySeverity(advisory.reason),
                              ).bg,
                              color: statusColor(
                                advisorySeverity(advisory.reason),
                              ).fg,
                              fontWeight: 500,
                              fontSize: 11,
                            }}
                          />
                        ) : null}
                      </Box>
                    }
                  >
                    <ListItemIcon sx={{ minWidth: 32 }}>
                      <Checkbox
                        edge="start"
                        size="small"
                        checked={checked}
                        onChange={() => toggleColumn(key)}
                      />
                    </ListItemIcon>
                    <ListItemText
                      primary={
                        <Typography
                          variant="body2"
                          sx={{ fontFamily: "monospace", fontSize: 12 }}
                        >
                          <span style={{ color: "gray", fontSize: 10 }}>
                            [{c.tableLabel}]{" "}
                          </span>
                          {keyColumnName && isDimTable && (
                            <span style={{ color: "gray" }}>
                              {keyColumnName} {"-> "}
                            </span>
                          )}
                          {c.name}
                        </Typography>
                      }
                    />
                  </ListItem>
                );
              })}
            </List>
          )}

          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ display: "block", mt: 0.5 }}
          >
            {t("attributeRelationships.nullPolicyFixed")}
          </Typography>

          {bulkCreate.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t("attributeRelationships.createFailed")}
            </Alert>
          )}

          <Box display="flex" justifyContent="flex-end" gap={1} mt={1}>
            <Button size="small" onClick={resetForm}>
              {t("common.cancel")}
            </Button>
            <Button
              size="small"
              variant="outlined"
              disabled={selectedColumns.size === 0 || validating}
              startIcon={
                validating ? (
                  <CircularProgress size={14} />
                ) : (
                  <CheckCircleOutlineIcon fontSize="small" />
                )
              }
              onClick={handleValidate}
            >
              {t("attributeRelationships.validateSelection")}
            </Button>
            <Button
              size="small"
              variant="contained"
              disabled={selectedColumns.size === 0 || bulkCreate.isPending}
              onClick={() => bulkCreate.mutate()}
            >
              {bulkCreate.isPending ? (
                <CircularProgress size={16} />
              ) : (
                t("attributeRelationships.addSelected", {
                  count: selectedColumns.size,
                })
              )}
            </Button>
          </Box>
        </Paper>
      )}
    </Box>
  );
}
