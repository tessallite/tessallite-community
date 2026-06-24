/**
 * KpiFormulaEditor — Enhanced textarea with function picker sidebar,
 * debounced validation, template dropdown, and live preview.
 *
 * Follows the same pattern as SqlQueryEditor (textarea + Paper wrapper +
 * action row) and AttributesTab (function picker with insert-to-textarea).
 * No external editor dependency.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import RuleIcon from "@mui/icons-material/Rule";
import { useT } from "../../../i18n";
import { kpisApi } from "../../../api/client";
import type {
  Dimension,
  Kpi,
  KpiValidationResponse,
  Measure,
} from "../../../api/types";
import type { KpiWizardFormState } from "../types";
import KpiPreviewCard from "../KpiPreviewCard";
import FunctionPicker from "./FunctionPicker";
import { EXPRESSION_TEMPLATES } from "./expressionTemplates";

interface Props {
  form: KpiWizardFormState;
  expression: string;
  targetExpression: string;
  onChange: (expression: string) => void;
  projectId: string;
  modelId: string;
  measures: Measure[];
  dimensions: Dimension[];
  kpis: Kpi[];
  readOnly?: boolean;
}

export default function KpiFormulaEditor({
  form,
  expression,
  targetExpression,
  onChange,
  projectId,
  modelId,
  measures,
  dimensions,
  kpis,
  readOnly = false,
}: Props) {
  const t = useT();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [validation, setValidation] = useState<KpiValidationResponse | null>(null);
  const [validating, setValidating] = useState(false);
  const [templateId, setTemplateId] = useState("");

  // --- Debounced validation (300ms) ---
  useEffect(() => {
    if (!expression.trim() || !projectId || !modelId) {
      setValidation(null);
      return;
    }
    let cancelled = false;
    setValidating(true);

    const handle = setTimeout(async () => {
      try {
        const res = await kpisApi.validateExpression(projectId, modelId, {
          expression,
          direction: form.direction,
        });
        if (!cancelled) setValidation(res);
      } catch {
        if (!cancelled) {
          setValidation({
            valid: false,
            errors: [{ code: "network", message: t("kpis.formula.validationNetworkError"), position: null, suggestion: null }],
            warnings: [],
            referenced_measures: [],
            referenced_kpis: [],
            referenced_dimensions: [],
            has_time_intelligence: false,
            requires_time_dimension: false,
            detected_agg_mode: null,
            expression_tree: null,
            compiled_sql_preview: null,
          });
        }
      } finally {
        if (!cancelled) setValidating(false);
      }
    }, 300);

    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [expression, projectId, modelId, form.direction, t]);

  // --- Insert text at cursor ---
  const insertAtCursor = useCallback(
    (snippet: string) => {
      const ta = textareaRef.current;
      if (!ta) {
        // No ref, just append
        onChange(expression ? `${expression}\n${snippet}` : snippet);
        return;
      }
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      const before = expression.slice(0, start);
      const after = expression.slice(end);
      const next = before + snippet + after;
      onChange(next);
      // Restore cursor after the inserted text
      requestAnimationFrame(() => {
        ta.focus();
        const pos = start + snippet.length;
        ta.setSelectionRange(pos, pos);
      });
    },
    [expression, onChange],
  );

  // --- Template selection ---
  const handleTemplateChange = (id: string) => {
    setTemplateId(id);
    const tpl = EXPRESSION_TEMPLATES.find((t) => t.id === id);
    if (tpl) onChange(tpl.expression);
  };

  // --- Copy ---
  const handleCopy = () => {
    void navigator.clipboard?.writeText(expression);
  };

  const hasErrors = validation && !validation.valid;
  const isValid = validation?.valid === true;

  return (
    <Box sx={{ display: "flex", gap: 2, minHeight: 300 }}>
      {/* Left sidebar — function picker */}
      <Paper
        variant="outlined"
        sx={{ width: 260, flexShrink: 0, p: 1.5, overflow: "auto" }}
      >
        <Typography variant="caption" fontWeight={600} sx={{ mb: 1, display: "block" }}>
          {t("kpis.formula.functionPickerTitle")}
        </Typography>
        <FunctionPicker onInsert={insertAtCursor} />
      </Paper>

      {/* Right side — textarea + toolbar + validation + preview */}
      <Box sx={{ flex: 1, display: "flex", flexDirection: "column", gap: 1.5 }}>
        {/* Toolbar row */}
        <Box display="flex" alignItems="center" gap={1}>
          <FormControl size="small" sx={{ minWidth: 200 }}>
            <InputLabel>{t("kpis.formula.templateLabel")}</InputLabel>
            <Select
              value={templateId}
              label={t("kpis.formula.templateLabel")}
              onChange={(e) => handleTemplateChange(e.target.value)}
              disabled={readOnly}
            >
              {EXPRESSION_TEMPLATES.map((tpl) => (
                <MenuItem key={tpl.id} value={tpl.id}>
                  {t(tpl.labelKey) || tpl.labelFallback}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          <Box flexGrow={1} />

          {/* Validation status indicator */}
          {validating && <CircularProgress size={16} />}
          {!validating && isValid && (
            <Tooltip title={t("kpis.formula.validExpression")}>
              <CheckCircleOutlineIcon fontSize="small" color="success" />
            </Tooltip>
          )}
          {!validating && hasErrors && (
            <Tooltip title={t("kpis.formula.invalidExpression")}>
              <ErrorOutlineIcon fontSize="small" color="error" />
            </Tooltip>
          )}

          <Tooltip title={t("kpis.formula.copyExpression")}>
            <span>
              <IconButton size="small" onClick={handleCopy} disabled={!expression.trim()}>
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Box>

        {/* Expression textarea (SqlQueryEditor pattern) */}
        <Paper
          variant="outlined"
          sx={{
            p: 0,
            "& textarea": {
              fontFamily: "JetBrains Mono, monospace",
              fontSize: 13,
              padding: "10px",
              border: "none",
              outline: "none",
              width: "100%",
              minHeight: 120,
              resize: "vertical",
              boxSizing: "border-box",
              backgroundColor: "transparent",
              color: "#212121",
            },
          }}
        >
          <textarea
            ref={textareaRef}
            value={expression}
            onChange={(e) => onChange(e.target.value)}
            spellCheck={false}
            placeholder={t("kpis.formula.placeholder")}
            readOnly={readOnly}
          />
        </Paper>

        {/* Validation results */}
        {validating && (
          <Typography variant="caption" color="text.secondary">
            {t("kpis.formula.validating")}
          </Typography>
        )}

        {!validating && isValid && (
          <Alert severity="success" sx={{ py: 0.25 }}>
            {t("kpis.formula.validExpression")}
            {validation.referenced_measures.length > 0 && (
              <Typography variant="caption" display="block" mt={0.5}>
                {t("kpis.formula.referencedMeasures")}: {validation.referenced_measures.join(", ")}
              </Typography>
            )}
          </Alert>
        )}

        {!validating && hasErrors && validation.errors.length > 0 && (
          <Alert severity="error" sx={{ py: 0.25 }}>
            {validation.errors.map((e, i) => (
              <Typography key={i} variant="caption" display="block">
                {e.message}
                {e.suggestion && (
                  <Button
                    size="small"
                    sx={{ ml: 1, textTransform: "none", fontSize: 11, py: 0 }}
                    onClick={() => {
                      if (e.suggestion) onChange(e.suggestion);
                    }}
                  >
                    {t("kpis.formula.applySuggestion")}
                  </Button>
                )}
              </Typography>
            ))}
          </Alert>
        )}

        {!validating && validation && validation.warnings.length > 0 && (
          <Alert severity="warning" sx={{ py: 0.25 }}>
            {validation.warnings.map((w, i) => (
              <Typography key={i} variant="caption" display="block">
                {w.message}
              </Typography>
            ))}
          </Alert>
        )}

        {/* Live preview */}
        <KpiPreviewCard
          form={form}
          expression={expression}
          targetExpression={targetExpression}
          projectId={projectId}
          modelId={modelId}
        />
      </Box>
    </Box>
  );
}
