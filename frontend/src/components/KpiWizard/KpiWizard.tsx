/**
 * KpiWizard — Main orchestrator for the KPI v2 wizard dialog.
 *
 * Manages step navigation, form state, expression generation,
 * live validation, and save/update logic.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Step,
  StepButton,
  Stepper,
} from "@mui/material";
import { useT } from "../../i18n";
import { kpisApi } from "../../api/client";
import { recordCreate, recordUpdate } from "../Builder/emitDrawerHistory";
import type {
  Dimension,
  Kpi,
  KpiCreate,
  KpiValidationResponse,
  Measure,
  VersionEntry,
} from "../../api/types";
import type { KpiWizardFormState, WizardStep } from "./types";
import { EMPTY_FORM } from "./types";
import { buildExpression, buildTargetExpression } from "./expressionBuilder";
import CodeIcon from "@mui/icons-material/Code";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";
import KpiWizardStep1TypeInputs from "./KpiWizardStep1TypeInputs";
import KpiWizardStep2TargetDirection from "./KpiWizardStep2TargetDirection";
import KpiWizardStep3Thresholds from "./KpiWizardStep3Thresholds";
import KpiWizardStep4Display from "./KpiWizardStep4Display";
import KpiWizardStep5Review from "./KpiWizardStep5Review";
import KpiPreviewCard from "./KpiPreviewCard";
import { KpiFormulaEditor } from "./FormulaEditor";

/** Template data used to pre-fill the wizard in create mode. */
export interface KpiTemplateInit {
  name: string;
  display_name: string;
  description: string;
  display_folder: string;
  expression: string;
  kpi_type: string;
  direction: string;
  status_graphic: string;
  trend_graphic: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
  projectId: string;
  modelId: string;
  measures: Measure[];
  dimensions: Dimension[];
  kpis: Kpi[];
  /** Pre-filled form for editing. Null = create mode. */
  editKpi: Kpi | null;
  /** Template data to pre-fill in create mode. Cleared after first open. */
  initialTemplate?: KpiTemplateInit | null;
  /** Admin flag for governance. */
  isAdmin: boolean;
  canEdit: boolean;
}

const STEP_LABELS = [
  "kpis.wizard.v2.stepType",
  "kpis.wizard.v2.stepTarget",
  "kpis.wizard.v2.stepThresholds",
  "kpis.wizard.v2.stepDisplay",
  "kpis.wizard.v2.stepReview",
] as const;

function kpiToFormState(kpi: Kpi, nullDefault = "N/A", t: (key: string) => string): KpiWizardFormState {
  return {
    name: kpi.name,
    display_name: kpi.display_name ?? "",
    description: kpi.description ?? "",
    display_folder: kpi.display_folder ?? "",
    kpi_type: kpi.kpi_type ?? "",
    expression: kpi.expression ?? "",
    calc_agg_mode: kpi.calc_agg_mode ?? "automatic",
    inner_agg: kpi.inner_agg ?? "",
    inner_grain: kpi.inner_grain ?? "",
    outer_agg: kpi.outer_agg ?? "",
    target_type: kpi.target_type ?? "",
    target_value: kpi.target_value != null ? String(kpi.target_value) : "",
    target_measure_id: kpi.target_measure_id ?? "",
    target_expression: kpi.target_expression ?? "",
    target_period: kpi.target_period ?? "",
    direction: kpi.direction ?? "higher_is_better",
    presentation_type: kpi.presentation_type ?? "",
    presentation_meta: kpi.presentation_meta ?? null,
    trend_period: kpi.trend_period ?? "month",
    trend_threshold: kpi.trend_threshold != null ? String(kpi.trend_threshold) : "0.01",
    trend_sparkline_periods: kpi.trend_sparkline_periods != null ? String(kpi.trend_sparkline_periods) : "12",
    format_token: kpi.format_token ?? "",
    format_custom: kpi.format_custom ?? "",
    unit_label: kpi.unit_label ?? "",
    null_display_value: kpi.null_display_value ?? nullDefault,
    weight: kpi.weight != null ? String(kpi.weight) : "",
    parent_kpi_id: kpi.parent_kpi_id ?? "",
    indicator_type: kpi.indicator_type ?? "none",
    time_dimension_id: kpi.time_dimension_id ?? "",
    snapshot_frequency: kpi.snapshot_frequency ?? "",
    snapshot_retention: kpi.snapshot_retention != null ? String(kpi.snapshot_retention) : "90",
    certification_status: kpi.certification_status ?? "draft",
    owner_user_id: kpi.owner_user_id ?? "",
    primaryMeasure: "",
    secondaryMeasure: "",
    status_graphic: kpi.status_graphic ?? t("kpis.wizard.v2.statusTrafficLight"),
    trend_graphic: kpi.trend_graphic ?? t("kpis.wizard.v2.statusStandardArrow"),
  };
}

/** Derive a technical name (slug) from a free-text display name. */
function slugify(s: string): string {
  return (
    s
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "") || "kpi"
  );
}

export default function KpiWizard({
  open,
  onClose,
  onSaved,
  projectId,
  modelId,
  measures,
  dimensions,
  kpis,
  editKpi,
  initialTemplate,
  isAdmin,
  canEdit,
}: Props) {
  const t = useT();
  const tRef = useRef(t);
  tRef.current = t;

  const editMode = editKpi !== null;

  const [step, setStep] = useState<WizardStep>(0);
  const [form, setForm] = useState<KpiWizardFormState>(EMPTY_FORM);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [editorMode, setEditorMode] = useState<"wizard" | "formula">("wizard");

  // Validation
  const [validation, setValidation] = useState<KpiValidationResponse | null>(null);
  const [validationLoading, setValidationLoading] = useState(false);

  // Version history (edit mode)
  const [versions, setVersions] = useState<VersionEntry[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);

  // Original template expression (preserved for measure-mapping UI)
  const [templateExpression, setTemplateExpression] = useState<string | undefined>(undefined);

  // Governance mutation states
  const [certifyPending, setCertifyPending] = useState(false);
  const [deprecatePending, setDeprecatePending] = useState(false);
  const [revertPending, setRevertPending] = useState(false);

  // Reset form when dialog opens or editKpi changes
  useEffect(() => {
    if (open) {
      setStep(0);
      setEditorMode("wizard");
      setError(null);
      setValidation(null);
      setVersions([]);
      if (editKpi) {
        setForm(kpiToFormState(editKpi, t("kpis.wizard.v2.nullDisplayDefault"), t));
        setTemplateExpression(undefined);
      } else if (initialTemplate) {
        setForm({
          ...EMPTY_FORM,
          name: initialTemplate.name,
          display_name: initialTemplate.display_name,
          description: initialTemplate.description,
          display_folder: initialTemplate.display_folder,
          expression: initialTemplate.expression,
          kpi_type: initialTemplate.kpi_type as KpiWizardFormState["kpi_type"],
          direction: (initialTemplate.direction as KpiWizardFormState["direction"]) || "higher_is_better",
          status_graphic: initialTemplate.status_graphic,
          trend_graphic: initialTemplate.trend_graphic,
        });
        setTemplateExpression(initialTemplate.expression);
      } else {
        setForm({ ...EMPTY_FORM, null_display_value: t("kpis.wizard.v2.nullDisplayDefault") });
        setTemplateExpression(undefined);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editKpi, initialTemplate]);

  // Load version history in edit mode
  useEffect(() => {
    if (open && editMode && editKpi) {
      setVersionsLoading(true);
      kpisApi
        .versions(projectId, modelId, editKpi.id)
        .then(setVersions)
        .catch(() => setVersions([]))
        .finally(() => setVersionsLoading(false));
    }
  }, [open, editMode, editKpi, projectId, modelId]);

  const handleChange = useCallback((patch: Partial<KpiWizardFormState>) => {
    setForm((prev) => ({ ...prev, ...patch }));
  }, []);

  // Generated expressions
  const generatedExpression = useMemo(
    () => form.expression || buildExpression(form, measures),
    [form, measures],
  );

  const generatedTargetExpression = useMemo(
    () => form.target_expression && form.target_type === "expression"
      ? form.target_expression
      : buildTargetExpression(form, measures),
    [form, measures],
  );

  // Validate expression when reaching review step
  useEffect(() => {
    if (step !== 4 || !generatedExpression || !projectId || !modelId) return;
    setValidationLoading(true);
    kpisApi
      .validateExpression(projectId, modelId, {
        expression: generatedExpression,
        target_expression: generatedTargetExpression || undefined,
        direction: form.direction,
      })
      .then(setValidation)
      .catch(() =>
        setValidation({
          valid: false,
          errors: [{ code: "NETWORK_ERROR", message: tRef.current("kpis.wizard.v2.validationNetworkError"), position: null, suggestion: null }],
          warnings: [],
          referenced_measures: [],
          referenced_kpis: [],
          referenced_dimensions: [],
          has_time_intelligence: false,
          requires_time_dimension: false,
          detected_agg_mode: null,
          expression_tree: null,
          compiled_sql_preview: null,
        }),
      )
      .finally(() => setValidationLoading(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, generatedExpression, generatedTargetExpression, form.direction, projectId, modelId]);

  // Step gating
  const hasType = Boolean(form.kpi_type);
  const hasExpression = Boolean(generatedExpression);
  const hasName = Boolean(form.display_name.trim() || form.name.trim());

  function canEnterStep(target: number): boolean {
    if (target === 0) return true;
    if (target >= 1 && !hasType) return false;
    if (target >= 4 && !hasName) return false;
    return true;
  }

  // Save
  async function handleSave() {
    setSaving(true);
    setError(null);

    const finalName = editMode
      ? form.name
      : form.name.trim() || slugify(form.display_name);

    const payload: Record<string, unknown> = {
      name: finalName,
      display_name: form.display_name || undefined,
      description: form.description || undefined,
      display_folder: form.display_folder || undefined,
      kpi_type: form.kpi_type || undefined,
      expression: generatedExpression || undefined,
      calc_agg_mode: form.calc_agg_mode,
      inner_agg: form.calc_agg_mode === "aggregate_of_aggregate" && form.inner_agg ? form.inner_agg : undefined,
      inner_grain: form.calc_agg_mode === "aggregate_of_aggregate" && form.inner_grain ? form.inner_grain : undefined,
      outer_agg: form.calc_agg_mode === "aggregate_of_aggregate" && form.outer_agg ? form.outer_agg : undefined,
      target_type: form.target_type || undefined,
      target_value: form.target_type === "static" && form.target_value ? Number(form.target_value) : undefined,
      target_measure_id: form.target_type === "measure" && form.target_measure_id ? form.target_measure_id : undefined,
      target_expression: generatedTargetExpression || undefined,
      target_period: form.target_period || undefined,
      direction: form.direction,
      presentation_type: form.presentation_type || undefined,
      presentation_meta: form.presentation_meta ?? undefined,
      trend_period: form.trend_period || undefined,
      trend_threshold: form.trend_threshold ? Number(form.trend_threshold) : undefined,
      trend_sparkline_periods: form.trend_sparkline_periods ? Number(form.trend_sparkline_periods) : undefined,
      format_token: form.format_token || undefined,
      format_custom: form.format_token === "custom" ? form.format_custom : undefined,
      unit_label: form.unit_label || undefined,
      null_display_value: form.null_display_value || undefined,
      weight: form.weight ? Number(form.weight) : undefined,
      parent_kpi_id: form.parent_kpi_id || undefined,
      indicator_type: form.indicator_type !== "none" ? form.indicator_type : undefined,
      time_dimension_id: form.time_dimension_id || undefined,
      snapshot_frequency: form.snapshot_frequency || undefined,
      snapshot_retention: form.snapshot_frequency && form.snapshot_retention ? Number(form.snapshot_retention) : undefined,
      status_graphic: form.status_graphic,
      trend_graphic: form.trend_graphic,
    };

    try {
      if (editMode && editKpi) {
        if (form.certification_status) {
          payload.certification_status = form.certification_status;
        }
        // Bug-8227: build the inverse payload from the prior KPI, touching
        // exactly the fields this update sends, so undo restores the prior
        // definition without disturbing unrelated fields.
        const priorRecord = editKpi as unknown as Record<string, unknown>;
        const priorPayload: Record<string, unknown> = {};
        for (const key of Object.keys(payload)) {
          // Preserve null explicitly so undo actively resets a newly-set
          // field (null->value) back to null rather than omitting it.
          priorPayload[key] = priorRecord[key] !== undefined ? priorRecord[key] : null;
        }
        await kpisApi.update(projectId, modelId, editKpi.id, payload);
        recordUpdate("kpi", editKpi.id, priorPayload, payload);
      } else {
        const created = await kpisApi.create(projectId, modelId, payload as unknown as KpiCreate);
        recordCreate("kpi", created.id, payload);
      }
      onSaved();
      onClose();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      setError(typeof detail === "string" ? detail : JSON.stringify(detail) ?? t("kpis.saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  // Governance handlers
  const handleCertify = useCallback(
    async (id: string) => {
      setCertifyPending(true);
      try {
        await kpisApi.certify(projectId, modelId, id);
        onSaved();
        onClose();
      } catch (err: any) {
        setError(err?.response?.data?.detail ?? t("kpis.certifyFailed"));
      } finally {
        setCertifyPending(false);
      }
    },
    [projectId, modelId, onSaved, onClose],
  );

  const handleDeprecate = useCallback(
    async (id: string, replacementId?: string) => {
      setDeprecatePending(true);
      try {
        await kpisApi.deprecate(projectId, modelId, id, { replacement_id: replacementId || null });
        onSaved();
        onClose();
      } catch (err: any) {
        setError(err?.response?.data?.detail ?? t("kpis.deprecateFailed"));
      } finally {
        setDeprecatePending(false);
      }
    },
    [projectId, modelId, onSaved, onClose],
  );

  const handleRevert = useCallback(
    async (id: string, versionNumber: number) => {
      setRevertPending(true);
      try {
        await kpisApi.revert(projectId, modelId, id, versionNumber);
        onSaved();
        onClose();
      } catch (err: any) {
        setError(err?.response?.data?.detail ?? t("kpis.revertFailed"));
      } finally {
        setRevertPending(false);
      }
    },
    [projectId, modelId, onSaved, onClose],
  );

  // Time-intelligence expressions evaluate via period-bounded queries and
  // cannot run without a time dimension — block save until one is picked.
  const needsTimeDimension =
    Boolean(validation?.requires_time_dimension || validation?.has_time_intelligence) &&
    !form.time_dimension_id;
  const canSubmit = hasName && hasExpression && !needsTimeDimension;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pb: 1, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        {editMode ? t("kpis.editTitle") : t("kpis.addTitle")}
        <Button
          size="small"
          variant="text"
          startIcon={editorMode === "wizard" ? <CodeIcon /> : <AutoFixHighIcon />}
          onClick={() => setEditorMode((m) => (m === "wizard" ? "formula" : "wizard"))}
        >
          {editorMode === "wizard" ? t("kpis.formula.switchToEditor") : t("kpis.formula.switchToWizard")}
        </Button>
      </DialogTitle>

      {editorMode === "wizard" && (
        <Box sx={{ px: 3, pt: 1 }}>
          <Stepper activeStep={step} alternativeLabel nonLinear>
            {STEP_LABELS.map((labelKey, i) => (
              <Step key={labelKey}>
                <StepButton
                  color="inherit"
                  onClick={() => setStep(i as WizardStep)}
                  disabled={!canEnterStep(i)}
                >
                  {t(labelKey)}
                </StepButton>
              </Step>
            ))}
          </Stepper>
        </Box>
      )}

      <DialogContent dividers sx={{ minHeight: 340 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}

        {editorMode === "formula" ? (
          <KpiFormulaEditor
            form={form}
            expression={generatedExpression}
            targetExpression={generatedTargetExpression}
            onChange={(expr) => handleChange({ expression: expr })}
            projectId={projectId}
            modelId={modelId}
            measures={measures}
            dimensions={dimensions}
            kpis={kpis}
          />
        ) : (
          <>
            {/* Live preview (shown on steps 0-3) */}
            {step < 4 && generatedExpression && (
              <Box sx={{ mb: 2 }}>
                <KpiPreviewCard
                  form={form}
                  expression={generatedExpression}
                  targetExpression={generatedTargetExpression}
                  projectId={projectId}
                  modelId={modelId}
                />
              </Box>
            )}

            {step === 0 && (
              <KpiWizardStep1TypeInputs
                form={form}
                onChange={handleChange}
                measures={measures}
                generatedExpression={generatedExpression}
                templateExpression={templateExpression}
              />
            )}
            {step === 1 && (
              <KpiWizardStep2TargetDirection
                form={form}
                onChange={handleChange}
                measures={measures}
              />
            )}
            {step === 2 && (
              <KpiWizardStep3Thresholds form={form} onChange={handleChange} />
            )}
            {step === 3 && (
              <KpiWizardStep4Display
                form={form}
                onChange={handleChange}
                kpis={kpis}
                dimensions={dimensions}
              />
            )}
            {step === 4 && (
              <KpiWizardStep5Review
                form={form}
                measures={measures}
                kpis={kpis}
                generatedExpression={generatedExpression}
                generatedTargetExpression={generatedTargetExpression}
                validation={validation}
                validationLoading={validationLoading}
                editMode={editMode}
                editId={editKpi?.id ?? null}
                projectId={projectId}
                modelId={modelId}
                versions={versions}
                versionsLoading={versionsLoading}
                isAdmin={isAdmin}
                canEdit={canEdit}
                onCertify={handleCertify}
                onDeprecate={handleDeprecate}
                onRevert={handleRevert}
                certifyPending={certifyPending}
                deprecatePending={deprecatePending}
                revertPending={revertPending}
              />
            )}
          </>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2, justifyContent: "space-between" }}>
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Box display="flex" gap={1}>
          {editorMode === "wizard" && step > 0 && (
            <Button onClick={() => setStep((s) => (s - 1) as WizardStep)}>
              {t("kpis.wizard.back")}
            </Button>
          )}
          {editorMode === "wizard" && step < 4 ? (
            <Button
              variant="contained"
              disabled={!canEnterStep(step + 1)}
              onClick={() => setStep((s) => (s + 1) as WizardStep)}
            >
              {t("kpis.wizard.next")}
            </Button>
          ) : (
            <Button
              variant="contained"
              disabled={saving || !canSubmit}
              onClick={handleSave}
            >
              {saving ? (
                <CircularProgress size={16} />
              ) : editMode ? (
                t("common.save")
              ) : (
                t("common.create")
              )}
            </Button>
          )}
        </Box>
      </DialogActions>
    </Dialog>
  );
}
