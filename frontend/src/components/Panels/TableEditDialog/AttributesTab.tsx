import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Divider,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import EditIcon from "@mui/icons-material/Edit";
import DeleteIcon from "@mui/icons-material/Delete";
import ScienceIcon from "@mui/icons-material/Science";
import { tableAttributesApi, userDefinedAttributesApi } from "../../../api/client";
import { useDataTags } from "../../../api/hooks";
import type {
  ModelTable,
  TableAttribute,
  UserDefinedAttribute,
  UserDefinedAttributeFunctionOption,
} from "../../../api/types";
import SyncColumnsButton from "./SyncColumnsButton";
import { useT } from "../../../i18n";

type OutputType = "varchar" | "integer" | "numeric" | "date";

interface Props {
  projectId: string;
  modelId: string;
  table: ModelTable;
  connectionId: string | null;
}

export default function AttributesTab({ projectId, modelId, table, connectionId }: Props) {
  const t = useT();
  const qc = useQueryClient();

  const attributes = useQuery({
    queryKey: ["tableAttributes", projectId, modelId, table.id],
    queryFn: () => tableAttributesApi.list(projectId, modelId, table.id),
    staleTime: 20 * 1000,
  });

  const udaList = useQuery({
    queryKey: ["userDefinedAttributes", projectId, modelId, table.id],
    queryFn: () => userDefinedAttributesApi.list(projectId, modelId, table.id),
    staleTime: 20 * 1000,
  });

  const functionCatalog = useQuery({
    queryKey: ["userDefinedAttributeFunctionCatalog", projectId, modelId, table.id],
    queryFn: () => userDefinedAttributesApi.functionCatalog(projectId, modelId, table.id),
    staleTime: 5 * 60 * 1000,
  });

  // F-008-08: tagged columns show their data-tag chips next to the name,
  // as documented in the Data Tags help page.
  const dataTags = useDataTags(projectId, modelId);
  const tagNamesByColumnId = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const tag of dataTags.data ?? []) {
      for (const c of tag.columns) {
        const list = map.get(c.column_id) ?? [];
        list.push(tag.tag_name);
        map.set(c.column_id, list);
      }
    }
    return map;
  }, [dataTags.data]);

  const [editingAttrId, setEditingAttrId] = useState<string | null>(null);
  const [editingIsGenerated, setEditingIsGenerated] = useState(false);
  const [name, setName] = useState("");
  const [expression, setExpression] = useState("");
  const [outputType, setOutputType] = useState<OutputType>("varchar");
  const [description, setDescription] = useState("");
  const [selectedFunction, setSelectedFunction] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [validateMsg, setValidateMsg] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<TableAttribute | null>(null);

  const invalidateAttrs = () => {
    qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId, table.id] });
    qc.invalidateQueries({ queryKey: ["userDefinedAttributes", projectId, modelId, table.id] });
    qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
  };

  const validateFormula = useMutation({
    mutationFn: () =>
      userDefinedAttributesApi.validate(projectId, modelId, table.id, {
        expression,
        output_data_type: outputType,
      }),
    onSuccess: (res) => {
      if (res.parse_valid && res.columns_resolved && res.live_validation.success) {
        setValidateMsg(t("tableEditAttrs.validationPassed"));
        setLocalError(null);
      } else {
        setLocalError(res.live_validation.error ?? t("tableEditAttrs.validationFailed"));
        setValidateMsg(null);
      }
    },
    onError: () => {
      setLocalError(t("tableEditAttrs.validationFailed"));
      setValidateMsg(null);
    },
  });

  const createUda = useMutation({
    mutationFn: () =>
      userDefinedAttributesApi.create(projectId, modelId, table.id, {
        name,
        expression,
        output_data_type: outputType,
        description: description || undefined,
      }),
    onSuccess: () => {
      invalidateAttrs();
      clearForm();
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setLocalError(err?.response?.data?.detail ?? t("tableEditAttrs.createAttrFailed"));
    },
  });

  const updateUda = useMutation({
    mutationFn: () =>
      userDefinedAttributesApi.update(projectId, modelId, table.id, editingAttrId!, {
        name,
        expression,
        output_data_type: outputType,
        description: description || undefined,
      }),
    onSuccess: () => {
      invalidateAttrs();
      clearForm();
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setLocalError(err?.response?.data?.detail ?? t("tableEditAttrs.updateAttrFailed"));
    },
  });

  const deleteAttribute = useMutation({
    mutationFn: ({ attr }: { attr: TableAttribute }) =>
      tableAttributesApi.delete(
        projectId,
        modelId,
        table.id,
        attr.id,
        attr.is_user_defined ? "user_defined" : "physical",
      ),
    onSuccess: () => invalidateAttrs(),
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setLocalError(err?.response?.data?.detail ?? t("tableEditAttrs.removeAttrFailed"));
    },
  });

  function clearForm() {
    setEditingAttrId(null);
    setEditingIsGenerated(false);
    setName("");
    setExpression("");
    setOutputType("varchar");
    setDescription("");
    setSelectedFunction("");
    setValidateMsg(null);
    setLocalError(null);
  }

  function insertFunctionTemplate() {
    const fn = functionCatalog.data?.find(
      (item: UserDefinedAttributeFunctionOption) => item.name === selectedFunction,
    );
    if (!fn) return;
    setExpression((prev) => (prev.trim() ? `${prev}\n${fn.template}` : fn.template));
    setLocalError(null);
  }

  function startEdit(attrId: string) {
    const uda = udaList.data?.find((a: UserDefinedAttribute) => a.id === attrId);
    if (!uda) {
      setLocalError(t("tableEditAttrs.loadAttrError"));
      return;
    }
    setEditingAttrId(attrId);
    setEditingIsGenerated(Boolean(uda.is_generated));
    setName(uda.name);
    setExpression(uda.expression);
    setOutputType(uda.output_data_type);
    setDescription(uda.description ?? "");
    setValidateMsg(null);
    setLocalError(null);
  }

  function onSubmit() {
    if (!name || !expression) {
      setLocalError(t("tableEditAttrs.nameExpressionRequired"));
      return;
    }
    if (editingAttrId) updateUda.mutate();
    else createUda.mutate();
  }

  const physicalCount = (attributes.data ?? []).filter((a) => !a.is_user_defined).length;
  const udaCount = (attributes.data ?? []).filter((a) => a.is_user_defined).length;

  return (
    <Stack spacing={2} sx={{ mt: 1 }}>
      {localError && (
        <Alert severity="error" onClose={() => setLocalError(null)}>{localError}</Alert>
      )}
      {validateMsg && (
        <Alert severity="success" onClose={() => setValidateMsg(null)}>{validateMsg}</Alert>
      )}

      {physicalCount === 0 && (
        <Alert
          severity="info"
          action={
            <SyncColumnsButton
              projectId={projectId}
              modelId={modelId}
              table={table}
              connectionId={connectionId}
              variant="text"
              label={t("tableEditAttrs.syncColumnsLabel")}
            />
          }
        >
          {t("tableEditAttrs.noPhysicalColumnsAlert")}
        </Alert>
      )}

      {/* Existing attributes list */}
      <Box>
        <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
          {t("tableEditAttrs.existingAttrsTitle")}
          {udaCount > 0 && (
            <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
              {t("tableEditAttrs.computedCount", {
                computed: String(udaCount),
                physical: String(physicalCount),
              })}
            </Typography>
          )}
        </Typography>

        <TableContainer
          sx={{ border: 1, borderColor: "divider", borderRadius: 1, maxHeight: 180 }}
        >
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                  {t("tableEditAttrs.nameHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                  {t("tableEditAttrs.expressionHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 80 }}>
                  {t("tableEditAttrs.kindHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 80, textAlign: "right" }}>
                  {t("tableEditAttrs.actionsHeader")}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(attributes.data ?? []).length === 0 && (
                <TableRow>
                  <TableCell colSpan={4} sx={{ color: "text.secondary", py: 2 }}>
                    {t("tableEditAttrs.noAttributes")}
                  </TableCell>
                </TableRow>
              )}
              {attributes.data?.map((attr) => (
                <TableRow key={attr.id} hover>
                  <TableCell sx={{ py: 0.5 }}>
                    <Typography
                      variant="body2"
                      sx={{
                        fontFamily: attr.is_user_defined ? "monospace" : "inherit",
                        fontStyle: attr.is_user_defined ? "italic" : "normal",
                        fontSize: "0.8rem",
                        color: attr.is_user_defined ? "secondary.main" : "text.primary",
                      }}
                    >
                      {attr.is_user_defined ? t("attributes.computedPrefix") : ""}{attr.name}
                      {(tagNamesByColumnId.get(attr.id) ?? []).map((tagName) => (
                        <Tooltip key={tagName} title={t("tableEditAttrs.dataTagTooltip")}>
                          <Chip
                            label={tagName}
                            size="small"
                            variant="outlined"
                            sx={{ ml: 0.5, height: 18, fontSize: "0.65rem" }}
                          />
                        </Tooltip>
                      ))}
                    </Typography>
                  </TableCell>
                  <TableCell sx={{ py: 0.5 }}>
                    {attr.expression ? (
                      <Typography
                        variant="caption"
                        sx={{ fontFamily: "monospace", color: "text.secondary" }}
                      >
                        {attr.expression}
                      </Typography>
                    ) : (
                      <Typography variant="caption" color="text.disabled">
                        {t("common.na")}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell
                    sx={{
                      fontSize: "0.75rem",
                      color: attr.is_user_defined ? "secondary.main" : "text.secondary",
                      py: 0.5,
                    }}
                  >
                    {attr.is_user_defined ? t("tableEditAttrs.kindComputed") : t("tableEditAttrs.kindPhysical")}
                  </TableCell>
                  <TableCell sx={{ textAlign: "right", py: 0.5 }}>
                    {attr.is_user_defined && (
                      <Tooltip title={t("tableEditAttrs.editFormulaTooltip")}>
                        <IconButton size="small" onClick={() => startEdit(attr.id)}>
                          <EditIcon sx={{ fontSize: 16 }} />
                        </IconButton>
                      </Tooltip>
                    )}
                    <Tooltip title={t("tableEditAttrs.removeTooltip")}>
                      <IconButton
                        size="small"
                        onClick={() => setDeleteTarget(attr)}
                        disabled={deleteAttribute.isPending}
                      >
                        <DeleteIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      </Box>

      <Divider />

      {/* Add / edit form */}
      <Box>
        <Typography variant="subtitle2" sx={{ mb: 1 }}>
          {editingAttrId ? t("tableEditAttrs.editComputedTitle") : t("tableEditAttrs.addComputedTitle")}
        </Typography>

        <Stack spacing={1.5}>
          <TextField
            label={t("tableEditAttrs.nameLabel")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            fullWidth
            size="small"
          />
          <TextField
            label={t("tableEditAttrs.expressionLabel")}
            value={expression}
            onChange={(e) => setExpression(e.target.value)}
            fullWidth
            multiline
            minRows={3}
            size="small"
            placeholder={t("tableEditAttrs.expressionPlaceholder")}
            disabled={editingIsGenerated}
            helperText={editingIsGenerated ? t("tableEditAttrs.generatedExpressionHint") : undefined}
          />
          {!editingIsGenerated && (
            <Box sx={{ display: "flex", gap: 1 }}>
              <FormControl size="small" fullWidth>
                <InputLabel>{t("tableEditAttrs.functionLabel")}</InputLabel>
                <Select
                  value={selectedFunction}
                  label={t("tableEditAttrs.functionLabel")}
                  onChange={(e) => setSelectedFunction(String(e.target.value))}
                >
                  {(functionCatalog.data ?? []).map((fn) => (
                    <MenuItem key={fn.name} value={fn.name}>
                      {fn.name}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <Button variant="outlined" onClick={insertFunctionTemplate} disabled={!selectedFunction}>
                {t("tableEditAttrs.insertButton")}
              </Button>
            </Box>
          )}
          {!editingIsGenerated && selectedFunction && (
            <Typography variant="caption" color="text.secondary">
              {(functionCatalog.data ?? []).find((f) => f.name === selectedFunction)?.signature}
              {t("attributes.signatureSeparator")}
              {(functionCatalog.data ?? []).find((f) => f.name === selectedFunction)?.description}
            </Typography>
          )}
          <Box sx={{ display: "flex", gap: 1 }}>
            <FormControl size="small" sx={{ minWidth: 140 }}>
              <InputLabel>{t("tableEditAttrs.outputTypeLabel")}</InputLabel>
              <Select
                value={outputType}
                label={t("tableEditAttrs.outputTypeLabel")}
                onChange={(e) => setOutputType(e.target.value as OutputType)}
              >
                <MenuItem value="varchar">{t("attributes.typeVarchar")}</MenuItem>
                <MenuItem value="integer">{t("attributes.typeInteger")}</MenuItem>
                <MenuItem value="numeric">{t("attributes.typeNumeric")}</MenuItem>
                <MenuItem value="date">{t("attributes.typeDate")}</MenuItem>
              </Select>
            </FormControl>
            <TextField
              label={t("tableEditAttrs.descriptionLabel")}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              fullWidth
              size="small"
              placeholder={t("tableEditAttrs.descriptionPlaceholder")}
            />
          </Box>

          <Box display="flex" justifyContent="flex-end" gap={1}>
            {editingAttrId && (
              <Button onClick={clearForm} size="small">
                {t("common.cancel")}
              </Button>
            )}
            <Button
              startIcon={<ScienceIcon sx={{ fontSize: 16 }} />}
              onClick={() => validateFormula.mutate()}
              disabled={!expression || editingIsGenerated || validateFormula.isPending}
              size="small"
            >
              {validateFormula.isPending ? <CircularProgress size={14} /> : t("tableEditAttrs.validateButton")}
            </Button>
            <Button
              variant="contained"
              onClick={onSubmit}
              disabled={createUda.isPending || updateUda.isPending}
              size="small"
            >
              {editingAttrId ? t("tableEditAttrs.updateButton") : t("tableEditAttrs.addButton")}
            </Button>
          </Box>
        </Stack>
      </Box>

      <Dialog open={deleteTarget !== null} onClose={() => setDeleteTarget(null)}>
        <DialogTitle>{t("tableEditAttrs.removeDialogTitle")}</DialogTitle>
        <DialogContent>
          <DialogContentText>
            {t("tableEditAttrs.removeDialogMessage", { name: deleteTarget?.name ?? "" })}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteTarget(null)} size="small">
            {t("common.cancel")}
          </Button>
          <Button
            variant="contained"
            size="small"
            disabled={deleteAttribute.isPending}
            onClick={() => {
              if (deleteTarget) {
                deleteAttribute.mutate({ attr: deleteTarget });
                setDeleteTarget(null);
              }
            }}
          >
            {deleteAttribute.isPending ? <CircularProgress size={14} /> : t("tableEditAttrs.removeButton")}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
