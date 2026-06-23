/**
 * KpiPreviewCard — Live KPI evaluation preview.
 *
 * Calls the evaluate-adhoc endpoint with the current wizard form state
 * to show a real-time preview of the KPI value and status.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Typography,
} from "@mui/material";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import WarningIcon from "@mui/icons-material/Warning";
import ErrorIcon from "@mui/icons-material/Error";
import { useT } from "../../i18n";
import { kpisApi } from "../../api/client";
import type { KpiEvaluateResponse } from "../../api/types";
import type { KpiWizardFormState } from "./types";
import Sparkline from "../KpiScorecard/Sparkline";

interface Props {
  form: KpiWizardFormState;
  expression: string;
  targetExpression: string;
  projectId: string;
  modelId: string;
}

function StatusIcon({ status }: { status: number | null }) {
  if (status === null) return null;
  if (status >= 1) return <CheckCircleIcon fontSize="small" color="success" />;
  if (status === 0) return <WarningIcon fontSize="small" color="warning" />;
  return <ErrorIcon fontSize="small" color="error" />;
}

export default function KpiPreviewCard({
  form,
  expression,
  targetExpression,
  projectId,
  modelId,
}: Props) {
  const t = useT();
  const tRef = useRef(t);
  tRef.current = t;

  const [result, setResult] = useState<KpiEvaluateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Stable JSON key for presentation_meta to avoid re-fires on every render
  const metaJson = JSON.stringify(form.presentation_meta ?? null);

  const evaluate = useCallback(async () => {
    if (!expression || !projectId || !modelId) {
      setResult(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await kpisApi.evaluateAdhoc(projectId, modelId, {
        expression,
        target_expression: targetExpression || undefined,
        direction: form.direction,
        format_token: form.format_token || undefined,
        format_custom: form.format_custom || undefined,
        unit_label: form.unit_label || undefined,
        presentation_meta: form.presentation_meta ?? undefined,
        time_dimension: form.time_dimension_id || undefined,
      });
      setResult(res);
    } catch (err: any) {
      setError(err?.response?.data?.detail ?? tRef.current("kpis.wizard.v2.previewError"));
      setResult(null);
    } finally {
      setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expression, targetExpression, form.direction, form.format_token, form.format_custom, form.unit_label, form.time_dimension_id, metaJson, projectId, modelId]);

  // Debounced evaluation on expression change
  useEffect(() => {
    const timer = setTimeout(evaluate, 600);
    return () => clearTimeout(timer);
  }, [evaluate]);

  if (!expression) return null;

  return (
    <Card variant="outlined" sx={{ bgcolor: "action.hover" }}>
      <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
        <Typography variant="caption" fontWeight={600} sx={{ textTransform: "uppercase", mb: 0.5, display: "block" }}>
          {t("kpis.wizard.v2.livePreview")}
        </Typography>

        {error && (
          <Typography variant="caption" color="error">
            {error}
          </Typography>
        )}

        {result && (
          <Box display="flex" gap={1} flexWrap="wrap" alignItems="center" sx={loading ? { opacity: 0.5 } : undefined}>
            <Chip
              size="small"
              label={`${t("kpis.wizard.v2.previewValue")}: ${result.formatted_value ?? (result.value !== null && result.value !== undefined ? String(result.value) : t("kpis.wizard.v2.previewNoData"))}`}
              variant="outlined"
            />
            {(result.formatted_target || result.target !== null && result.target !== undefined) && (
              <Chip size="small" label={`${t("kpis.wizard.v2.targetValue")}: ${result.formatted_target ?? String(result.target)}`} variant="outlined" />
            )}
            {result.status_label && (
              <Chip
                size="small"
                icon={<StatusIcon status={result.status} />}
                label={result.status_label}
                sx={result.status_color ? { borderColor: result.status_color, color: result.status_color } : {}}
                variant="outlined"
              />
            )}
            {result.formatted_variance && (
              <Chip size="small" label={result.formatted_variance} variant="outlined" />
            )}
            {/* F-017-26: live sparkline from the ad-hoc trend series. */}
            {result.trend_series && result.trend_series.length > 1 && (
              <Sparkline data={result.trend_series} trend={result.trend ?? null} />
            )}
            {loading && <CircularProgress size={14} />}
          </Box>
        )}

        {!result && loading && <CircularProgress size={16} />}
      </CardContent>
    </Card>
  );
}
