import { useState } from "react";
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
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";

import { parametersApi } from "../../api/client";
import type {
  ModelParameter,
  ModelParameterCreate,
  ParamType,
} from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";

type DialogMode = "create" | "edit";

interface FormState {
  name: string;
  display_name: string;
  param_type: ParamType;
  default_value: string;
  allowed_values: string;
  description: string;
}

const EMPTY: FormState = {
  name: "@",
  display_name: "",
  param_type: "string",
  default_value: "",
  allowed_values: "",
  description: "",
};

const PARAM_TYPES: { value: ParamType; i18nKey: string }[] = [
  { value: "string", i18nKey: "parameters.typeString" },
  { value: "number", i18nKey: "parameters.typeNumber" },
  { value: "boolean", i18nKey: "parameters.typeBoolean" },
  { value: "multi_value", i18nKey: "parameters.typeMultiValue" },
  { value: "date_range", i18nKey: "parameters.typeDateRange" },
];

// F-029-04: mirror the backend `_PARAM_NAME_RE = ^@[A-Za-z_]\w*$` so `@1x` /
// `@region-code` fail inline instead of enabling Save then 422-ing.
export const PARAM_NAME_RE = /^@[A-Za-z_]\w*$/;

// F-029-05: mirror the backend boolean token set (resolver._coerce). The old
// `v.toLowerCase() === "true"` stored `false` for a modeller who typed `yes`
// or `1` — the opposite of the intended default.
const BOOLEAN_TRUE = new Set(["true", "1", "yes"]);
const BOOLEAN_FALSE = new Set(["false", "0", "no"]);

/** @internal exported for unit testing (Bug-7661 round-trip guard). */
export function parseDefaultValue(v: string, type: ParamType): unknown {
  if (!v.trim()) return undefined;
  if (type === "number") return Number(v);
  if (type === "boolean") {
    const n = v.trim().toLowerCase();
    if (BOOLEAN_TRUE.has(n)) return true;
    if (BOOLEAN_FALSE.has(n)) return false;
    return undefined; // invalid — gated by defaultValueError before Save
  }
  if (type === "multi_value") return v.split(",").map((s) => s.trim());
  if (type === "date_range") {
    try {
      return JSON.parse(v);
    } catch {
      return undefined;
    }
  }
  return v;
}

/** F-029-05: return an i18n key for an invalid default (or null when valid),
 *  so Save is blocked and the field shows a reason instead of silently storing
 *  a corrupted value. Matches the backend coercion contract. */
export function defaultValueError(v: string, type: ParamType): string | null {
  if (!v.trim()) return null; // no default is allowed
  if (type === "number") {
    return Number.isFinite(Number(v)) ? null : "parameters.defaultValueNumberError";
  }
  if (type === "boolean") {
    const n = v.trim().toLowerCase();
    return BOOLEAN_TRUE.has(n) || BOOLEAN_FALSE.has(n)
      ? null
      : "parameters.defaultValueBooleanError";
  }
  if (type === "date_range") {
    let parsed: unknown;
    try {
      parsed = JSON.parse(v);
    } catch {
      return "parameters.defaultValueDateRangeError";
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as Record<string, unknown>).from !== "string" ||
      typeof (parsed as Record<string, unknown>).to !== "string"
    ) {
      return "parameters.defaultValueDateRangeError";
    }
  }
  return null;
}

/** @internal exported for unit testing (Bug-7661 round-trip guard). */
export function formatDefaultValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  // Bug-7661: multi_value defaults are stored as arrays (e.g. ["EMEA","NA"]).
  // Format them as comma-separated text so parseDefaultValue (which comma-
  // splits) round-trips without corruption.  JSON.stringify would produce
  // '["EMEA","NA"]' which comma-splits into '["EMEA"' and '"NA"]'.
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export default function ParametersPanel() {
  const { projectId = "", modelId = "" } = useParams();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const storeReadOnly = useBuilderStore((s) => s.readOnly);
  const canEdit = !storeReadOnly;  // Bug-8784: backend caller_can_author is authoritative
  const t = useT();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<DialogMode>("create");
  const [editId, setEditId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [error, setError] = useState<string | null>(null);

  const queryKey = ["parameters", projectId, modelId];

  const { data: params = [], isLoading } = useQuery({
    queryKey,
    queryFn: () => parametersApi.list(projectId, modelId),
    enabled: Boolean(projectId && modelId),
  });

  const createMut = useMutation({
    mutationFn: (data: ModelParameterCreate) =>
      parametersApi.create(projectId, modelId, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("parameters.createError")),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      parametersApi.update(projectId, modelId, id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("parameters.updateError")),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => parametersApi.delete(projectId, modelId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
  });

  function openCreate() {
    setForm(EMPTY);
    setDialogMode("create");
    setEditId(null);
    setError(null);
    setDialogOpen(true);
  }

  function openEdit(p: ModelParameter) {
    setForm({
      name: p.name,
      display_name: p.display_name ?? "",
      param_type: p.param_type,
      default_value: formatDefaultValue(p.default_value),
      allowed_values: Array.isArray(p.allowed_values)
        ? p.allowed_values.join(", ")
        : "",
      description: p.description ?? "",
    });
    setDialogMode("edit");
    setEditId(p.id);
    setError(null);
    setDialogOpen(true);
  }

  function closeDialog() {
    setDialogOpen(false);
    setError(null);
  }

  function handleSave() {
    const defaultVal = parseDefaultValue(form.default_value, form.param_type);
    const allowedVals = form.allowed_values.trim()
      ? form.allowed_values.split(",").map((s) => s.trim())
      : undefined;

    if (dialogMode === "create") {
      createMut.mutate({
        name: form.name,
        display_name: form.display_name || undefined,
        param_type: form.param_type,
        default_value: defaultVal,
        allowed_values: allowedVals,
        description: form.description || undefined,
      });
    } else if (editId) {
      updateMut.mutate({
        id: editId,
        data: {
          // Send explicit null (not undefined) so cleared fields actually
          // reset. undefined is dropped from the JSON body and the backend's
          // exclude_unset PATCH would keep the old value, making the clear a
          // silent no-op (Bug-6414). false/0 defaults are preserved via ??.
          display_name: form.display_name || null,
          param_type: form.param_type,
          default_value: defaultVal ?? null,
          allowed_values: allowedVals ?? null,
          description: form.description || null,
        },
      });
    }
  }

  async function handleDelete(p: ModelParameter) {
    const ok = await confirm({
      title: t("parameters.deleteConfirmTitle"),
      message: t("parameters.deleteConfirmMessage", { name: p.name }),
      confirmLabel: t("common.delete"),
    });
    if (ok) deleteMut.mutate(p.id);
  }

  const isPending = createMut.isPending || updateMut.isPending;
  // F-029-04: enforce the backend name grammar inline.
  const nameValid = PARAM_NAME_RE.test(form.name);
  // F-029-05: block Save on a default that would be silently corrupted or 422.
  const defaultValErrorKey = defaultValueError(form.default_value, form.param_type);

  return (
    <Box sx={{ p: 2, overflow: "auto" }}>
      <Box display="flex" alignItems="center" justifyContent="space-between" mb={2}>
        <Typography variant="subtitle1" fontWeight={700}>
          {t("parameters.title")}
        </Typography>
        {canEdit && (
          <Button
            size="small"
            startIcon={<AddIcon />}
            variant="contained"
            onClick={openCreate}
          >
            {t("parameters.addButton")}
          </Button>
        )}
      </Box>

      <Typography variant="body2" color="text.secondary" mb={2}>
        {t("parameters.description")}
      </Typography>

      {isLoading && <CircularProgress size={20} />}

      {!isLoading && params.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("parameters.emptyMessage")}
        </Typography>
      )}

      <Stack spacing={1.5}>
        {params.map((p: ModelParameter) => (
          <Card key={p.id} variant="outlined">
            <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
              <Box display="flex" alignItems="center" justifyContent="space-between">
                <Box>
                  <Typography variant="subtitle2" fontFamily="monospace">
                    {p.name}
                  </Typography>
                  {p.display_name && (
                    <Typography variant="caption" color="text.secondary">
                      {p.display_name}
                    </Typography>
                  )}
                </Box>
                <Box display="flex" alignItems="center" gap={0.5}>
                  <Chip label={p.param_type} size="small" variant="outlined" />
                  {canEdit && (
                    <>
                      <Button size="small" startIcon={<EditIcon />} onClick={() => openEdit(p)}>
                        {t("common.edit")}
                      </Button>
                      <Button
                        size="small"
                        startIcon={<DeleteIcon />}
                        onClick={() => handleDelete(p)}
                      >
                        {t("common.delete")}
                      </Button>
                    </>
                  )}
                </Box>
              </Box>
              {p.description && (
                <Typography variant="body2" color="text.secondary" mt={0.5}>
                  {p.description}
                </Typography>
              )}
              <Box display="flex" gap={2} mt={1}>
                <Typography variant="caption" color="text.secondary">
                  {t("parameters.defaultLabel")}: {p.default_value !== null ? formatDefaultValue(p.default_value) : t("parameters.noneValue")}
                </Typography>
                {p.allowed_values && (
                  <Typography variant="caption" color="text.secondary">
                    {t("parameters.allowedLabel")}: {Array.isArray(p.allowed_values) ? p.allowed_values.join(", ") : t("common.separator")}
                  </Typography>
                )}
              </Box>
            </CardContent>
          </Card>
        ))}
      </Stack>

      <Dialog open={dialogOpen} onClose={closeDialog} maxWidth="sm" fullWidth>
        <DialogTitle>
          {dialogMode === "create" ? t("parameters.createDialogTitle") : t("parameters.editDialogTitle")}
        </DialogTitle>
        <DialogContent>
          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}
          <TextField
            label={t("parameters.nameLabel")}
            fullWidth
            margin="normal"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            disabled={dialogMode === "edit"}
            placeholder={t("parameters.namePlaceholder")}
            helperText={t("parameters.nameHelperText")}
            error={form.name.length > 0 && !nameValid}
          />
          <TextField
            label={t("parameters.displayNameLabel")}
            fullWidth
            margin="normal"
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            placeholder={t("parameters.displayNamePlaceholder")}
          />
          <TextField
            select
            label={t("parameters.typeLabel")}
            fullWidth
            margin="normal"
            value={form.param_type}
            onChange={(e) => setForm({ ...form, param_type: e.target.value as ParamType })}
          >
            {PARAM_TYPES.map((pt) => (
              <MenuItem key={pt.value} value={pt.value}>
                {t(pt.i18nKey)}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label={t("parameters.defaultValueLabel")}
            fullWidth
            margin="normal"
            value={form.default_value}
            onChange={(e) => setForm({ ...form, default_value: e.target.value })}
            error={Boolean(defaultValErrorKey)}
            helperText={
              defaultValErrorKey
                ? t(defaultValErrorKey)
                : form.param_type === "date_range"
                  ? t("parameters.defaultValueDateRangeHelper")
                  : form.param_type === "multi_value"
                    ? t("parameters.defaultValueMultiHelper")
                    : undefined
            }
          />
          <TextField
            label={t("parameters.allowedValuesLabel")}
            fullWidth
            margin="normal"
            value={form.allowed_values}
            onChange={(e) => setForm({ ...form, allowed_values: e.target.value })}
            helperText={t("parameters.allowedValuesHelper")}
          />
          <TextField
            label={t("parameters.descriptionLabel")}
            fullWidth
            margin="normal"
            multiline
            rows={2}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={closeDialog}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            disabled={isPending || !nameValid || Boolean(defaultValErrorKey)}
            onClick={handleSave}
          >
            {isPending ? <CircularProgress size={16} /> : dialogMode === "create" ? t("common.create") : t("common.save")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
