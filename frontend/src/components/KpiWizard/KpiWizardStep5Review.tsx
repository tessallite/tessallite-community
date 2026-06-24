/**
 * KPI Wizard Step 5 — Review, validation, and governance.
 *
 * Shows a summary of all wizard inputs, live expression validation,
 * and governance controls (certify/deprecate) when editing.
 */
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  MenuItem,
  Tooltip,
  Typography,
} from "@mui/material";
import VerifiedIcon from "@mui/icons-material/Verified";
import BlockIcon from "@mui/icons-material/Block";
import HistoryIcon from "@mui/icons-material/History";
import RestoreIcon from "@mui/icons-material/Restore";
import { useT } from "../../i18n";
import type { Kpi, KpiValidationResponse, Measure, VersionEntry } from "../../api/types";
import type { KpiWizardFormState } from "./types";
import EntityImpactSummary from "../Panels/EntityImpactSummary";

interface Props {
  form: KpiWizardFormState;
  measures: Measure[];
  kpis: Kpi[];
  generatedExpression: string;
  generatedTargetExpression: string;
  validation: KpiValidationResponse | null;
  validationLoading: boolean;
  // Edit mode
  editMode: boolean;
  editId: string | null;
  projectId: string;
  modelId: string;
  // Version history
  versions: VersionEntry[];
  versionsLoading: boolean;
  // Governance callbacks
  isAdmin: boolean;
  canEdit: boolean;
  onCertify?: (id: string) => void;
  onDeprecate?: (id: string, replacementId?: string) => void;
  onRevert?: (id: string, versionNumber: number) => void;
  certifyPending?: boolean;
  deprecatePending?: boolean;
  revertPending?: boolean;
}

export default function KpiWizardStep5Review({
  form,
  measures,
  kpis,
  generatedExpression,
  generatedTargetExpression,
  validation,
  validationLoading,
  editMode,
  editId,
  projectId,
  modelId,
  versions,
  versionsLoading,
  isAdmin,
  canEdit,
  onCertify,
  onDeprecate,
  onRevert,
  certifyPending,
  deprecatePending,
  revertPending,
}: Props) {
  const t = useT();

  const measureName = (id: string) => {
    if (!id) return "\u2014";
    const m = measures.find((x) => x.id === id);
    return m ? (m.display_name || m.name) : id.slice(0, 8);
  };

  const bands = form.presentation_meta?.bands ?? [];

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
      <Typography variant="h6">{t("kpis.wizard.v2.reviewTitle")}</Typography>

      {/* Summary card */}
      <Card variant="outlined">
        <CardContent>
          <Typography variant="subtitle2">
            {form.display_name || form.name || t("kpis.wizard.v2.unnamed")}
          </Typography>
          {form.description && (
            <Typography variant="body2" color="text.secondary" mt={0.5}>
              {form.description}
            </Typography>
          )}

          <Divider sx={{ my: 1.5 }} />

          {/* Type & expression */}
          <Stack spacing={0.5}>
            <Typography variant="caption" fontWeight={600} sx={{ textTransform: "uppercase" }}>
              {t("kpis.wizard.v2.stepType")}
            </Typography>
            <Typography variant="body2">
              {form.kpi_type ? t(`kpis.wizard.v2.type${form.kpi_type.charAt(0).toUpperCase() + form.kpi_type.slice(1).replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())}`) : "\u2014"}
            </Typography>
            {generatedExpression && (
              <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: 12, bgcolor: "action.hover", p: 1, borderRadius: 1 }}>
                {generatedExpression}
              </Typography>
            )}
          </Stack>

          <Divider sx={{ my: 1.5 }} />

          {/* Target & direction */}
          <Stack spacing={0.5}>
            <Typography variant="caption" fontWeight={600} sx={{ textTransform: "uppercase" }}>
              {t("kpis.wizard.v2.stepTarget")}
            </Typography>
            <Box display="flex" gap={2} flexWrap="wrap">
              <Typography variant="body2">
                {t("kpis.wizard.v2.targetType")}: {form.target_type ? t(`kpis.targetType${form.target_type.charAt(0).toUpperCase() + form.target_type.slice(1).replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())}`) : t("kpis.none")}
              </Typography>
              <Typography variant="body2">
                {t("kpis.wizard.v2.direction")}: {t(`kpis.direction${form.direction.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase()).replace(/^[a-z]/, (c) => c.toUpperCase())}`)}
              </Typography>
            </Box>
            {generatedTargetExpression && (
              <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: 12, bgcolor: "action.hover", p: 1, borderRadius: 1 }}>
                {generatedTargetExpression}
              </Typography>
            )}
          </Stack>

          <Divider sx={{ my: 1.5 }} />

          {/* Thresholds */}
          <Stack spacing={0.5}>
            <Typography variant="caption" fontWeight={600} sx={{ textTransform: "uppercase" }}>
              {t("kpis.wizard.v2.stepThresholds")}
            </Typography>
            {bands.length > 0 ? (
              <Box display="flex" gap={0.5} flexWrap="wrap">
                {bands.map((band, i) => (
                  <Chip
                    key={i}
                    size="small"
                    label={`${t(band.label)}: ${band.min ?? t("kpis.wizard.v2.unboundedMin")} \u2013 ${band.max ?? t("kpis.wizard.v2.unboundedMax")}`}
                    sx={{
                      bgcolor: band.color,
                      color: "#fff",
                      fontWeight: 500,
                    }}
                  />
                ))}
              </Box>
            ) : (
              <Typography variant="body2" color="text.secondary">
                {t("kpis.wizard.v2.noThresholds")}
              </Typography>
            )}
          </Stack>

          <Divider sx={{ my: 1.5 }} />

          {/* Display settings */}
          <Stack spacing={0.5}>
            <Typography variant="caption" fontWeight={600} sx={{ textTransform: "uppercase" }}>
              {t("kpis.wizard.v2.stepDisplay")}
            </Typography>
            <Box display="flex" gap={2} flexWrap="wrap">
              {form.format_token && (
                <Typography variant="body2">
                  {t("kpis.wizard.v2.formatToken")}: {form.format_token}
                </Typography>
              )}
              {form.unit_label && (
                <Typography variant="body2">
                  {t("kpis.wizard.v2.unitLabel")}: {form.unit_label}
                </Typography>
              )}
              {form.display_folder && (
                <Typography variant="body2">
                  {t("kpis.displayFolder")}: {form.display_folder}
                </Typography>
              )}
              {form.parent_kpi_id && (
                <Typography variant="body2">
                  {t("kpis.parentKpi")}: {kpis.find((k) => k.id === form.parent_kpi_id)?.display_name ?? form.parent_kpi_id}
                </Typography>
              )}
            </Box>
          </Stack>
        </CardContent>
      </Card>

      {/* Validation results */}
      <Box>
        <Typography variant="subtitle2" mb={1}>
          {t("kpis.wizard.v2.validationTitle")}
        </Typography>
        {validationLoading && <CircularProgress size={20} />}
        {validation && !validationLoading && (
          <Alert severity={validation.valid ? "success" : "error"} sx={{ mb: 1 }}>
            {validation.valid
              ? t("kpis.wizard.v2.validationPass")
              : t("kpis.wizard.v2.validationFail")}
          </Alert>
        )}
        {validation &&
          !validationLoading &&
          (validation.requires_time_dimension || validation.has_time_intelligence) &&
          !form.time_dimension_id && (
            <Alert severity="warning" sx={{ mb: 1 }}>
              {t("kpis.wizard.v2.timeDimensionRequired")}
            </Alert>
          )}
        {validation && (validation.errors.length > 0 || validation.warnings.length > 0) && (
          <Stack spacing={0.5}>
            {validation.errors.map((d, i) => (
              <Alert key={`e-${i}`} severity="error" sx={{ py: 0 }}>
                {d.message}
                {d.suggestion && (
                  <Typography variant="caption" display="block" color="text.secondary">
                    {d.suggestion}
                  </Typography>
                )}
              </Alert>
            ))}
            {validation.warnings.map((d, i) => (
              <Alert key={`w-${i}`} severity="warning" sx={{ py: 0 }}>
                {d.message}
                {d.suggestion && (
                  <Typography variant="caption" display="block" color="text.secondary">
                    {d.suggestion}
                  </Typography>
                )}
              </Alert>
            ))}
          </Stack>
        )}
        {!validation && !validationLoading && (
          <Typography variant="body2" color="text.secondary">
            {t("kpis.wizard.v2.validationPending")}
          </Typography>
        )}
      </Box>

      {/* Governance (edit mode only) */}
      {editMode && editId && (
        <Box>
          <Divider sx={{ mb: 2 }} />
          <Stack direction="row" alignItems="center" spacing={1} mb={1}>
            <HistoryIcon fontSize="small" color="action" />
            <Typography variant="subtitle2">
              {t("kpis.wizard.governanceTitle")}
            </Typography>
          </Stack>

          {(form.certification_status === "shared" || form.certification_status === "certified") && (
            <EntityImpactSummary
              entityType="kpi"
              entityId={editId}
              entityName={form.display_name || form.name}
              projectId={projectId}
              modelId={modelId}
            />
          )}

          {isAdmin && (
            <Box display="flex" gap={1} mb={2} flexWrap="wrap">
              {form.certification_status !== "certified" && onCertify && (
                <Button
                  variant="outlined"
                  color="success"
                  size="small"
                  startIcon={<VerifiedIcon />}
                  disabled={certifyPending}
                  onClick={() => onCertify(editId)}
                >
                  {certifyPending ? <CircularProgress size={14} /> : t("kpis.certify")}
                </Button>
              )}
              {form.certification_status !== "deprecated" && onDeprecate && (
                <Button
                  variant="outlined"
                  color="warning"
                  size="small"
                  startIcon={<BlockIcon />}
                  disabled={deprecatePending}
                  onClick={() => onDeprecate(editId)}
                >
                  {deprecatePending ? <CircularProgress size={14} /> : t("kpis.deprecate")}
                </Button>
              )}
            </Box>
          )}

          <Typography variant="subtitle2" mb={1}>
            {t("kpis.versionHistory")}
          </Typography>
          {versionsLoading && <CircularProgress size={20} />}
          {!versionsLoading && versions.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              {t("kpis.noVersions")}
            </Typography>
          )}
          {versions.length > 0 && (
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{t("kpis.versionNumber")}</TableCell>
                  <TableCell>{t("kpis.changedBy")}</TableCell>
                  <TableCell>{t("kpis.date")}</TableCell>
                  <TableCell>{t("kpis.summary")}</TableCell>
                  <TableCell />
                </TableRow>
              </TableHead>
              <TableBody>
                {versions.map((v) => (
                  <TableRow key={v.id}>
                    <TableCell>{v.version_number}</TableCell>
                    <TableCell>{v.changed_by ?? "\u2014"}</TableCell>
                    <TableCell>
                      <Tooltip title={v.changed_at}>
                        <span>{new Date(v.changed_at).toLocaleDateString()}</span>
                      </Tooltip>
                    </TableCell>
                    <TableCell>{v.change_summary ?? "\u2014"}</TableCell>
                    <TableCell>
                      {canEdit && onRevert && (
                        <Button
                          size="small"
                          startIcon={<RestoreIcon />}
                          disabled={revertPending}
                          onClick={() => onRevert(editId, v.version_number)}
                        >
                          {t("kpis.revert")}
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Box>
      )}
    </Box>
  );
}
