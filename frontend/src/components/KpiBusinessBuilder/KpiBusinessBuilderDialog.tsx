import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";

import { useT } from "../../i18n";
import { kpisApi } from "../../api/client";
import type { Dimension, Kpi, Measure } from "../../api/types";
import type {
  BusinessDefinition,
  KpiCreate,
  KpiEvaluateResponse,
} from "../../api/types_domains/kpis";
import { FormulaPicker } from "./FormulaPicker";
import { KpiTimeWindowPicker } from "./KpiTimeWindowPicker";
import { KpiFilterBar } from "./KpiFilterBar";
import { KpiBusinessPreview } from "./KpiBusinessPreview";
import { KpiPresentationPicker } from "./KpiPresentationPicker";
import { KpiThresholdEditor, createDefaultPresentationMeta } from "./KpiThresholdEditor";
import {
  type BusinessBuilderForm,
  FORMULA_FAMILIES,
  TIME_WINDOW_PRESETS,
  createDefaultForm,
  definitionToForm,
  formToDefinition,
} from "./businessDefinition";

type TargetKind = "none" | "static" | "measure" | "expression";

const TARGET_OPTIONS: { value: TargetKind; labelKey: string }[] = [
  { value: "none", labelKey: "kpiBusiness.targetTypeNone" },
  { value: "static", labelKey: "kpiBusiness.targetTypeStatic" },
  { value: "measure", labelKey: "kpiBusiness.targetTypeMeasure" },
  { value: "expression", labelKey: "kpiBusiness.targetTypeExpression" },
];

const DIRECTION_OPTIONS = [
  { value: "higher_is_better", labelKey: "kpis.wizard.v2.directionHigher" },
  { value: "lower_is_better", labelKey: "kpis.wizard.v2.directionLower" },
  { value: "closer_is_better", labelKey: "kpis.wizard.v2.directionCloser" },
] as const;

function extractErrorMessage(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((e) => (typeof e === "object" && e ? (e as Record<string, unknown>).msg ?? (e as Record<string, unknown>).message ?? JSON.stringify(e) : String(e)))
      .slice(0, 5);
    return msgs.length ? msgs.join("; ") : null;
  }
  if (typeof detail === "object" && detail) {
    const d = detail as Record<string, unknown>;
    if (typeof d.message === "string" && Array.isArray(d.errors) && d.errors.length) {
      const errs = (d.errors as unknown[]).map((e) =>
        typeof e === "string" ? e : typeof e === "object" && e ? ((e as Record<string, unknown>).message as string) ?? JSON.stringify(e) : String(e),
      );
      return `${d.message}: ${errs.join("; ")}`;
    }
    if (typeof d.message === "string") return d.message;
    if (Array.isArray(d.errors)) {
      const errs = (d.errors as unknown[]).map((e) => (typeof e === "string" ? e : String(e)));
      return errs.join("; ");
    }
  }
  return null;
}

type Props = {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
  projectId: string;
  modelId: string;
  measures: Measure[];
  dimensions: Dimension[];
  editKpi?: Kpi | null;
  onOpenAdvanced?: (kpi: Kpi | null) => void;
};

export function KpiBusinessBuilderDialog({
  open,
  onClose,
  onSaved,
  projectId,
  modelId,
  measures,
  dimensions,
  editKpi,
  onOpenAdvanced,
}: Props) {
  const t = useT();
  const [form, setForm] = useState<BusinessBuilderForm>(createDefaultForm);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advancedWarning, setAdvancedWarning] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [previewResult, setPreviewResult] = useState<KpiEvaluateResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const timeDimensions = useMemo(
    () =>
      dimensions.filter(
        (d) =>
          d.is_time_dim ||
          (d.data_type ?? "").toLowerCase().includes("date") ||
          (d.data_type ?? "").toLowerCase().includes("timestamp"),
      ),
    [dimensions],
  );

  useEffect(() => {
    if (!open) return;
    if (editKpi?.business_definition) {
      const f = definitionToForm(editKpi.business_definition);
      f.name = editKpi.name;
      f.description = editKpi.description || "";
      f.displayFolder = editKpi.display_folder || "";
      f.formatToken = editKpi.format_token || "";
      f.unitLabel = editKpi.unit_label || "";
      f.trendPeriod = editKpi.trend_period || "month";
      f.direction = editKpi.direction || "higher_is_better";
      f.presentationType = editKpi.presentation_type || "";
      f.presentationMeta = editKpi.presentation_meta || null;
      if (!f.target) {
        if (editKpi.target_type === "static" && editKpi.target_value != null) {
          f.target = { type: "static", value: editKpi.target_value };
        } else if (editKpi.target_type === "measure" && editKpi.target_measure_id) {
          f.target = { type: "measure", measure_id: editKpi.target_measure_id };
        } else if (editKpi.target_type === "expression" && editKpi.target_expression) {
          f.target = { type: "expression", expression: editKpi.target_expression };
        }
      }
      setForm(f);
    } else {
      setForm(createDefaultForm());
    }
    setError(null);
    setPreviewResult(null);
    setAdvancedOpen(false);
    setAdvancedWarning(false);
  }, [open, editKpi]);

  const update = useCallback(
    (patch: Partial<BusinessBuilderForm>) => setForm((prev) => ({ ...prev, ...patch })),
    [],
  );

  const businessDef = useMemo(() => formToDefinition(form), [form]);
  const targetKind: TargetKind = form.target?.type ?? "none";

  const setTargetKind = useCallback((kind: TargetKind) => {
    if (kind === "none") {
      update({ target: null });
    } else if (kind === "static") {
      update({
        target: {
          type: "static",
          value: form.target?.type === "static" ? form.target.value : undefined,
        },
      });
    } else if (kind === "measure") {
      update({
        target: {
          type: "measure",
          measure_id: form.target?.type === "measure" ? form.target.measure_id : undefined,
        },
      });
    } else {
      update({
        target: {
          type: "expression",
          expression: form.target?.type === "expression" ? form.target.expression : "",
        },
      });
    }
  }, [form.target, update]);

  const summary = useMemo(() => {
    const parts: string[] = [];
    const ft = form.formula.type;
    const unknown = t("kpiBusiness.summaryUnknown");

    const compiled = businessDef._compiled;
    const tokens = compiled?.summary_tokens as Record<string, unknown> | undefined;

    if (ft === "single_measure") {
      const m = measures.find((x) => x.id === form.formula.measure_id);
      const name = m?.display_name || m?.name || (tokens?.measure_name as string) || unknown;
      const agg = form.formula.aggregation || "sum";
      const aggLabel = t(`kpiBusiness.agg${agg.charAt(0).toUpperCase()}${agg.slice(1)}`) || agg;
      parts.push(`${aggLabel} ${t("kpiBusiness.summaryOf")} ${name}`);
    } else if (ft === "ratio") {
      const n = measures.find((x) => x.id === form.formula.numerator_measure_id);
      const d = measures.find((x) => x.id === form.formula.denominator_measure_id);
      parts.push(`${n?.display_name || n?.name || (tokens?.numerator_name as string) || unknown} / ${d?.display_name || d?.name || (tokens?.denominator_name as string) || unknown}`);
    } else if (ft === "count_records") {
      parts.push(t("kpiBusiness.summaryCountRecords"));
    } else if (ft === "count_distinct") {
      const dim = dimensions.find((x) => x.id === form.formula.dimension_id);
      const name = dim?.display_name || dim?.name || (tokens?.dimension_name as string) || unknown;
      parts.push(`${t("kpiBusiness.summaryDistinctCount")} ${name}`);
    } else if (ft === "share_rank") {
      const m = measures.find((x) => x.id === form.formula.measure_id);
      const name = m?.display_name || m?.name || (tokens?.measure_name as string) || unknown;
      const st = form.formula.share_type || "share_of_total";
      parts.push(`${name} (${t(`kpiBusiness.${st === "rank" ? "rank" : st === "top_n_contribution" ? "topN" : "sharePercent"}`)})`);
    } else if (ft === "exception_sla") {
      const sla = form.formula.sla_type || "compliance_pct";
      parts.push(`${t("kpiBusiness.summarySlA")} (${t(`kpiBusiness.sla${sla === "compliance_pct" ? "CompliancePct" : sla === "exception_count" ? "BreachCount" : "Backlog"}`)})`);
    } else {
      const fam = FORMULA_FAMILIES.find((f) => f.type === ft);
      parts.push(fam ? t(fam.labelKey) : ft.replace(/_/g, " "));
    }

    if (form.timeWindow.preset) {
      const twEntry = TIME_WINDOW_PRESETS.find((tw) => tw.preset === form.timeWindow.preset);
      const twLabel = twEntry ? t(twEntry.labelKey) : form.timeWindow.preset.replace(/_/g, " ");
      parts.push(`${t("kpiBusiness.summaryFor")} ${twLabel}`);
    }

    if (form.filters.length > 0) {
      const labels = form.filters.map((f) => f.label || f.dimension_id).slice(0, 3);
      parts.push(`${t("kpiBusiness.summaryWhere")} ${labels.join("; ")}`);
    }
    return parts.join(" | ");
  }, [form, measures, dimensions, businessDef, t]);

  const handlePreview = useCallback(async () => {
    setPreviewLoading(true);
    setError(null);
    try {
      const result = await kpisApi.evaluateAdhoc(projectId, modelId, {
        business_definition: businessDef,
        direction: form.direction,
        format_token: (form.formatToken as KpiCreate["format_token"]) || undefined,
        unit_label: form.unitLabel || undefined,
        trend_period: form.trendPeriod || "month",
        presentation_meta: form.presentationMeta || undefined,
      });
      setPreviewResult(result);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      const msg = extractErrorMessage(detail) ?? t("errors.requestFailed");
      setError(msg);
      setPreviewResult(null);
    } finally {
      setPreviewLoading(false);
    }
  }, [projectId, modelId, businessDef, form, t]);

  const handleSave = useCallback(async () => {
    if (!form.name.trim()) {
      setError(t("kpiBusiness.nameRequired"));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload: KpiCreate = {
        name: form.name.trim(),
        display_name: form.name.trim(),
        description: form.description || undefined,
        display_folder: form.displayFolder || undefined,
        direction: form.direction,
        format_token: (form.formatToken as KpiCreate["format_token"]) || undefined,
        unit_label: form.unitLabel || undefined,
        trend_period: form.trendPeriod || "month",
        business_definition: businessDef,
        presentation_type: (form.presentationType as KpiCreate["presentation_type"]) || undefined,
        presentation_meta: form.presentationMeta || undefined,
      };

      if (form.target) {
        if (form.target.type === "static" && form.target.value != null) {
          payload.target_type = "static";
          payload.target_value = form.target.value;
        } else if (form.target.type === "measure" && form.target.measure_id) {
          payload.target_type = "measure";
          payload.target_measure_id = form.target.measure_id;
        } else if (form.target.type === "expression" && form.target.expression) {
          payload.target_type = "expression";
          payload.target_expression = form.target.expression;
        }
      } else if (editKpi) {
        payload.target_type = "none";
        payload.target_value = null;
        payload.target_measure_id = null;
        payload.target_expression = null;
      }

      if (editKpi) {
        await kpisApi.update(projectId, modelId, editKpi.id, payload);
      } else {
        await kpisApi.create(projectId, modelId, payload);
      }
      onSaved();
      onClose();
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })
        ?.response?.data?.detail;
      const msg = extractErrorMessage(detail) ?? t("errors.requestFailed");
      setError(msg);
    } finally {
      setSaving(false);
    }
  }, [form, businessDef, editKpi, projectId, modelId, onSaved, onClose, t]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth scroll="paper">
      <DialogTitle sx={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Typography variant="h6" component="span">
          {editKpi ? t("kpiBusiness.editTitle") : t("kpiBusiness.addTitle")}
        </Typography>
        <IconButton onClick={onClose} size="small">
          <CloseIcon />
        </IconButton>
      </DialogTitle>

      <DialogContent dividers sx={{ display: "flex", flexDirection: "column", gap: 3, pt: 2 }}>
        {/* Name */}
        <TextField
          label={t("kpiBusiness.name")}
          fullWidth
          size="small"
          value={form.name}
          onChange={(e) => update({ name: e.target.value })}
          placeholder={t("kpiBusiness.namePlaceholder")}
        />

        {/* Formula */}
        <FormulaPicker
          formula={form.formula}
          onChange={(formula) => update({ formula })}
          measures={measures}
          dimensions={dimensions}
        />

        {/* Time Window */}
        <KpiTimeWindowPicker
          timeWindow={form.timeWindow}
          onChange={(timeWindow) => update({ timeWindow })}
          timeDimensions={timeDimensions}
        />

        {/* Filters */}
        <KpiFilterBar
          filters={form.filters}
          onChange={(filters) => update({ filters })}
          dimensions={dimensions}
          projectId={projectId}
          modelId={modelId}
        />

        {/* Display type */}
        <Box
          sx={{
            border: (theme) => `1px solid ${theme.palette.divider}`,
            borderRadius: 1,
            p: 2,
            bgcolor: "background.paper",
          }}
        >
          <Typography variant="caption" color="text.secondary" display="block" mb={1.25}>
            {t("kpiBusiness.targetDirectionSection")}
          </Typography>
          <Stack spacing={2}>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
              <FormControl size="small" sx={{ flex: 1 }}>
                <InputLabel id="kpi-business-direction-label">
                  {t("kpis.wizard.v2.direction")}
                </InputLabel>
                <Select
                  labelId="kpi-business-direction-label"
                  id="kpi-business-direction"
                  value={form.direction}
                  label={t("kpis.wizard.v2.direction")}
                  onChange={(e) => {
                    const newDir = e.target.value as BusinessBuilderForm["direction"];
                    const patch: Partial<BusinessBuilderForm> = { direction: newDir };
                    if (form.presentationMeta && !form.bandsCustomized) {
                      const preservedType = form.presentationMeta.evaluation_type;
                      const targetForBands = preservedType === "percentage_of_target"
                        ? null
                        : (form.target?.type === "static" ? form.target.value : null);
                      patch.presentationMeta = createDefaultPresentationMeta(
                        targetForBands, newDir, preservedType,
                      );
                    }
                    update(patch);
                  }}
                >
                  {DIRECTION_OPTIONS.map((opt) => (
                    <MenuItem key={opt.value} value={opt.value}>
                      {t(opt.labelKey)}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>

              <FormControl size="small" sx={{ flex: 1 }}>
                <InputLabel id="kpi-business-target-type-label">
                  {t("kpiBusiness.targetType")}
                </InputLabel>
                <Select
                  labelId="kpi-business-target-type-label"
                  id="kpi-business-target-type"
                  value={targetKind}
                  label={t("kpiBusiness.targetType")}
                  onChange={(e) => setTargetKind(e.target.value as TargetKind)}
                >
                  {TARGET_OPTIONS.map((opt) => (
                    <MenuItem key={opt.value} value={opt.value}>
                      {t(opt.labelKey)}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            </Stack>

            {form.target?.type === "static" && (
              <TextField
                size="small"
                type="number"
                label={t("kpiBusiness.targetValue")}
                value={form.target.value ?? ""}
                onChange={(e) => {
                  const newVal = e.target.value === "" ? undefined : Number(e.target.value);
                  const patch: Partial<BusinessBuilderForm> = {
                    target: { type: "static", value: newVal },
                  };
                  if (form.presentationMeta && !form.bandsCustomized) {
                    const preservedType = form.presentationMeta.evaluation_type;
                    const targetForBands = preservedType === "percentage_of_target"
                      ? null
                      : (newVal ?? null);
                    patch.presentationMeta = createDefaultPresentationMeta(
                      targetForBands, form.direction, preservedType,
                    );
                  }
                  update(patch);
                }}
              />
            )}

            {form.target?.type === "measure" && (
              <FormControl size="small">
                <InputLabel id="kpi-business-target-measure-label">
                  {t("kpiBusiness.targetMeasure")}
                </InputLabel>
                <Select
                  labelId="kpi-business-target-measure-label"
                  id="kpi-business-target-measure"
                  value={form.target.measure_id ?? ""}
                  label={t("kpiBusiness.targetMeasure")}
                  onChange={(e) =>
                    update({
                      target: {
                        type: "measure",
                        measure_id: e.target.value || undefined,
                      },
                    })
                  }
                >
                  <MenuItem value="">{t("kpis.none")}</MenuItem>
                  {measures.map((m) => (
                    <MenuItem key={m.id} value={m.id}>
                      {m.display_name || m.name}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            )}

            {form.target?.type === "expression" && (
              <TextField
                size="small"
                multiline
                minRows={2}
                label={t("kpiBusiness.targetExpression")}
                value={form.target.expression ?? ""}
                onChange={(e) =>
                  update({
                    target: {
                      type: "expression",
                      expression: e.target.value,
                    },
                  })
                }
                sx={{
                  "& textarea": {
                    fontFamily: "monospace",
                    fontSize: 13,
                  },
                }}
              />
            )}
          </Stack>
        </Box>

        <KpiPresentationPicker
          value={form.presentationType}
          onChange={(v) =>
            update({
              presentationType: v,
              presentationMeta: v
                ? form.presentationMeta ??
                  createDefaultPresentationMeta(
                    form.target?.type === "static" ? form.target.value : null,
                    form.direction,
                  )
                : null,
            })
          }
        />

        {/* Threshold bands — every presentation type (including traffic light,
            whose lit lamp is driven by the status these bands define) uses them. */}
        {form.presentationType && (
          <KpiThresholdEditor
            meta={form.presentationMeta}
            direction={form.direction}
            target={form.target?.type === "static" ? form.target.value : null}
            onChange={(meta) => update({ presentationMeta: meta })}
            onBandsCustomized={() => update({ bandsCustomized: true })}
          />
        )}

        {/* Preview */}
        <Box>
          <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>
            {t("kpiBusiness.summary")}
          </Typography>
          <Typography variant="body2" sx={{ fontStyle: "italic", mb: 1 }}>
            {summary}
          </Typography>
          <KpiBusinessPreview
            result={previewResult}
            loading={previewLoading}
            onPreview={handlePreview}
          />
        </Box>

        {/* Error */}
        {error && (
          <Alert severity="error" onClose={() => setError(null)}>
            {error}
          </Alert>
        )}

        {/* Advanced section */}
        <Divider />
        <Box>
          <Button
            size="small"
            onClick={() => setAdvancedOpen(!advancedOpen)}
            endIcon={advancedOpen ? <ExpandLessIcon /> : <ExpandMoreIcon />}
          >
            {t("kpiBusiness.advanced")}
          </Button>
          <Collapse in={advancedOpen}>
            <Stack spacing={2} mt={1}>
              <TextField
                label={t("kpiBusiness.description")}
                fullWidth
                size="small"
                multiline
                rows={2}
                value={form.description}
                onChange={(e) => update({ description: e.target.value })}
              />
              <TextField
                label={t("kpiBusiness.displayFolder")}
                fullWidth
                size="small"
                value={form.displayFolder}
                onChange={(e) => update({ displayFolder: e.target.value })}
              />
              <Stack direction="row" spacing={2}>
                <TextField
                  label={t("kpiBusiness.formatToken")}
                  size="small"
                  value={form.formatToken}
                  onChange={(e) => update({ formatToken: e.target.value })}
                  sx={{ flex: 1 }}
                />
                <TextField
                  label={t("kpiBusiness.unitLabel")}
                  size="small"
                  value={form.unitLabel}
                  onChange={(e) => update({ unitLabel: e.target.value })}
                  sx={{ flex: 1 }}
                />
              </Stack>

              {/* Generated expression (read-only) */}
              {(previewResult?.compiled_expression || businessDef._compiled?.expression) && (
                <TextField
                  label={t("kpiBusiness.generatedExpression")}
                  fullWidth
                  size="small"
                  multiline
                  value={previewResult?.compiled_expression || businessDef._compiled?.expression || ""}
                  InputProps={{ readOnly: true }}
                />
              )}
              {previewResult?.compiled_scope && (
                <TextField
                  label={t("kpiBusiness.compiledScope")}
                  fullWidth
                  size="small"
                  multiline
                  minRows={3}
                  value={JSON.stringify(previewResult.compiled_scope, null, 2)}
                  InputProps={{ readOnly: true }}
                  sx={{
                    "& textarea": {
                      fontFamily: "monospace",
                      fontSize: 12,
                    },
                  }}
                />
              )}
              <TextField
                label={t("kpiBusiness.generatedDefinition")}
                fullWidth
                size="small"
                multiline
                minRows={4}
                value={JSON.stringify(businessDef, null, 2)}
                InputProps={{ readOnly: true }}
                sx={{
                  "& textarea": {
                    fontFamily: "monospace",
                    fontSize: 12,
                  },
                }}
              />
            </Stack>
          </Collapse>
        </Box>
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2, justifyContent: "space-between" }}>
        <Box>
          {onOpenAdvanced && (
            <>
              <Button
                size="small"
                startIcon={<OpenInNewIcon />}
                onClick={() => setAdvancedWarning(true)}
              >
                {t("kpiBusiness.openAdvancedEditor")}
              </Button>
              <Collapse in={advancedWarning}>
                <Alert severity="warning" sx={{ mt: 1 }} action={
                  <Button size="small" color="warning" onClick={() => {
                    onOpenAdvanced(editKpi ?? null);
                    onClose();
                  }}>
                    {t("common.continue")}
                  </Button>
                }>
                  {t("kpiBusiness.openAdvancedWarning")}
                </Alert>
              </Collapse>
            </>
          )}
        </Box>
        <Box display="flex" gap={1} flexShrink={0}>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={saving || !form.name.trim()}
            sx={{ whiteSpace: "nowrap" }}
          >
            {saving ? t("common.saving") : editKpi ? t("common.save") : t("kpiBusiness.create")}
          </Button>
        </Box>
      </DialogActions>
    </Dialog>
  );
}
