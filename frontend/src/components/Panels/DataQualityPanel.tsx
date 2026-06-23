import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { dataQualityApi } from "../../api/client";
import { useConfirm } from "../Confirm";
import type {
  DataQualityRule,
  DataQualityRuleCreate,
  DataQualityRuleType,
  DataQualityTargetType,
  DataQualitySeverity,
  DataQualityViolation,
} from "../../api/types";

const RULE_TYPES: DataQualityRuleType[] = ["not_null", "unique", "range", "regex", "custom_sql"];
const TARGET_TYPES: DataQualityTargetType[] = ["dimension", "measure", "column"];
const SEVERITIES: DataQualitySeverity[] = ["info", "warn", "error"];

const SEVERITY_COLOR: Record<DataQualitySeverity, "default" | "warning" | "error"> = {
  info: "default",
  warn: "warning",
  error: "error",
};

interface RuleFormState {
  name: string;
  target_type: DataQualityTargetType;
  target_id: string;
  rule_type: DataQualityRuleType;
  severity: DataQualitySeverity;
  is_enabled: boolean;
  block_on_failure: boolean;
  range_min: string;
  range_max: string;
  regex_pattern: string;
  custom_sql: string;
}

const EMPTY_FORM: RuleFormState = {
  name: "",
  target_type: "column",
  target_id: "",
  rule_type: "not_null",
  severity: "warn",
  is_enabled: true,
  block_on_failure: false,
  range_min: "",
  range_max: "",
  regex_pattern: "",
  custom_sql: "",
};

function getRuleTypeLabel(type: DataQualityRuleType, t: (key: string) => string): string {
  const labels: Record<DataQualityRuleType, string> = {
    not_null: t("dataQuality.ruleTypeNotNull"),
    unique: t("dataQuality.ruleTypeUnique"),
    range: t("dataQuality.ruleTypeRange"),
    regex: t("dataQuality.ruleTypeRegex"),
    custom_sql: t("dataQuality.ruleTypeCustomSql"),
  };
  return labels[type];
}

function getTargetTypeLabel(type: DataQualityTargetType, t: (key: string) => string): string {
  const labels: Record<DataQualityTargetType, string> = {
    dimension: t("dataQuality.targetTypeDimension"),
    measure: t("dataQuality.targetTypeMeasure"),
    column: t("dataQuality.targetTypeColumn"),
  };
  return labels[type];
}

function getSeverityLabel(severity: DataQualitySeverity, t: (key: string) => string): string {
  const labels: Record<DataQualitySeverity, string> = {
    info: t("dataQuality.severityInfo"),
    warn: t("dataQuality.severityWarn"),
    error: t("dataQuality.severityError"),
  };
  return labels[severity];
}

function buildRuleConfig(form: RuleFormState): Record<string, unknown> | null {
  if (form.rule_type === "range") {
    const cfg: Record<string, unknown> = {};
    if (form.range_min !== "") cfg.min = Number(form.range_min);
    if (form.range_max !== "") cfg.max = Number(form.range_max);
    return Object.keys(cfg).length > 0 ? cfg : null;
  }
  if (form.rule_type === "regex") {
    return form.regex_pattern ? { pattern: form.regex_pattern } : null;
  }
  if (form.rule_type === "custom_sql") {
    return form.custom_sql ? { sql: form.custom_sql } : null;
  }
  return null;
}

export default function DataQualityPanel() {
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();

  const { data: rules, isLoading } = useQuery<DataQualityRule[]>({
    queryKey: ["data-quality-rules", projectId, modelId],
    queryFn: () => dataQualityApi.list(projectId!, modelId!),
  });

  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [form, setForm] = useState<RuleFormState>(EMPTY_FORM);
  const [expandedRuleId, setExpandedRuleId] = useState<string | null>(null);
  const [ruleViolations, setRuleViolations] = useState<Record<string, DataQualityViolation[]>>({});
  const [validateResult, setValidateResult] = useState<{
    rules_checked: number;
    violations_found: number;
  } | null>(null);

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["data-quality-rules", projectId, modelId] });
  }

  const createRule = useMutation({
    mutationFn: () => {
      const payload: DataQualityRuleCreate = {
        name: form.name,
        target_type: form.target_type,
        target_id: form.target_id,
        rule_type: form.rule_type,
        severity: form.severity,
        is_enabled: form.is_enabled,
        block_on_failure: form.block_on_failure,
        rule_config: buildRuleConfig(form),
      };
      return dataQualityApi.create(projectId!, modelId!, payload);
    },
    onSuccess: () => {
      invalidate();
      setCreateDialogOpen(false);
      setForm(EMPTY_FORM);
    },
  });

  const toggleRule = useMutation({
    mutationFn: (rule: DataQualityRule) =>
      dataQualityApi.update(projectId!, modelId!, rule.id, { is_enabled: !rule.is_enabled }),
    onSuccess: invalidate,
  });

  const deleteRule = useMutation({
    mutationFn: (ruleId: string) => dataQualityApi.delete(projectId!, modelId!, ruleId),
    onSuccess: invalidate,
  });

  const runValidate = useMutation({
    mutationFn: () => dataQualityApi.validate(projectId!, modelId!),
    onSuccess: (res) => {
      setValidateResult({ rules_checked: res.rules_checked, violations_found: res.violations_found });
      invalidate();
    },
  });

  const clearViolations = useMutation({
    mutationFn: (ruleId: string) => dataQualityApi.clearViolations(projectId!, modelId!, ruleId),
    onSuccess: (_data, ruleId) => {
      setRuleViolations((prev) => {
        const next = { ...prev };
        delete next[ruleId];
        return next;
      });
      if (expandedRuleId) setExpandedRuleId(null);
      invalidate();
    },
  });

  async function handleDelete(rule: DataQualityRule) {
    const confirmed = await confirm({
      title: t("dataQuality.deleteRuleTitle"),
      message: t("dataQuality.deleteRuleMessage", { name: rule.name }),
    });
    if (confirmed) deleteRule.mutate(rule.id);
  }

  async function loadViolations(ruleId: string) {
    if (expandedRuleId === ruleId) {
      setExpandedRuleId(null);
      return;
    }
    setExpandedRuleId(ruleId);
    if (!ruleViolations[ruleId]) {
      const v = await dataQualityApi.listViolations(projectId!, modelId!, ruleId);
      setRuleViolations((prev) => ({ ...prev, [ruleId]: v }));
    }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" mb={1.5} spacing={1}>
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
          {t("dataQuality.panelDescription")}
        </Typography>
        <Button
          size="small"
          variant="outlined"
          startIcon={<PlayArrowIcon />}
          onClick={() => runValidate.mutate()}
          disabled={runValidate.isPending}
        >
          {runValidate.isPending ? <CircularProgress size={16} /> : t("dataQuality.runChecks")}
        </Button>
        <Button
          size="small"
          variant="outlined"
          startIcon={<AddIcon />}
          onClick={() => {
            setForm(EMPTY_FORM);
            setCreateDialogOpen(true);
          }}
        >
          {t("dataQuality.addRule")}
        </Button>
      </Stack>

      {validateResult && (
        <Alert
          severity={validateResult.violations_found > 0 ? "warning" : "success"}
          onClose={() => setValidateResult(null)}
          sx={{ mb: 1.5 }}
        >
          {t("dataQuality.validateResult", {
            rules: String(validateResult.rules_checked),
            violations: String(validateResult.violations_found),
          })}
        </Alert>
      )}

      {isLoading ? (
        <CircularProgress size={20} />
      ) : (rules ?? []).length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("dataQuality.noRulesDefined")}
        </Typography>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t("dataQuality.tableHeaderName")}</TableCell>
              <TableCell>{t("dataQuality.tableHeaderTargetType")}</TableCell>
              <TableCell>{t("dataQuality.tableHeaderRuleType")}</TableCell>
              <TableCell>{t("dataQuality.tableHeaderSeverity")}</TableCell>
              <TableCell>{t("dataQuality.tableHeaderBlock")}</TableCell>
              <TableCell>{t("dataQuality.tableHeaderLastCheck")}</TableCell>
              <TableCell>{t("dataQuality.tableHeaderViolations")}</TableCell>
              <TableCell />
            </TableRow>
          </TableHead>
          <TableBody>
            {(rules ?? []).map((rule) => (
              <>
                <TableRow key={rule.id} sx={{ opacity: rule.is_enabled ? 1 : 0.5 }}>
                  <TableCell>{rule.name}</TableCell>
                  <TableCell>{getTargetTypeLabel(rule.target_type, t)}</TableCell>
                  <TableCell>{getRuleTypeLabel(rule.rule_type, t)}</TableCell>
                  <TableCell>
                    <Chip
                      label={getSeverityLabel(rule.severity as DataQualitySeverity, t)}
                      size="small"
                      color={SEVERITY_COLOR[rule.severity as DataQualitySeverity] ?? "default"}
                    />
                  </TableCell>
                  <TableCell>
                    {rule.block_on_failure ? (
                      <Chip label={t("common.yes")} size="small" color="error" variant="outlined" />
                    ) : (
                      <Typography variant="caption" color="text.disabled">—</Typography>
                    )}
                  </TableCell>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    {rule.last_checked_at
                      ? new Date(rule.last_checked_at).toLocaleString()
                      : t("common.separator")}
                  </TableCell>
                  <TableCell>
                    {rule.last_violation_count ?? t("common.separator")}
                    {rule.last_violation_count != null && rule.last_violation_count > 0 && (
                      <Tooltip title={t("dataQuality.viewViolationHistory")}>
                        <IconButton size="small" onClick={() => loadViolations(rule.id)}>
                          {expandedRuleId === rule.id ? (
                            <ExpandLessIcon fontSize="small" />
                          ) : (
                            <ExpandMoreIcon fontSize="small" />
                          )}
                        </IconButton>
                      </Tooltip>
                    )}
                  </TableCell>
                  <TableCell align="right">
                    <Tooltip title={rule.is_enabled ? t("dataQuality.disableRule") : t("dataQuality.enableRule")}>
                      <Button
                        size="small"
                        variant="text"
                        onClick={() => toggleRule.mutate(rule)}
                      >
                        {rule.is_enabled ? t("dataQuality.disable") : t("dataQuality.enable")}
                      </Button>
                    </Tooltip>
                    {(rule.last_violation_count ?? 0) > 0 && (
                      <Tooltip title={t("dataQuality.clearViolationHistory")}>
                        <Button
                          size="small"
                          variant="text"
                          color="warning"
                          onClick={() => clearViolations.mutate(rule.id)}
                          disabled={clearViolations.isPending}
                        >
                          {t("dataQuality.clear")}
                        </Button>
                      </Tooltip>
                    )}
                    <Tooltip title={t("dataQuality.deleteRule")}>
                      <IconButton size="small" onClick={() => handleDelete(rule)}>
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
                {expandedRuleId === rule.id && (
                  <TableRow key={`${rule.id}-violations`}>
                    <TableCell colSpan={8} sx={{ py: 0 }}>
                      <Box sx={{ px: 2, pb: 1, pt: 0.5 }}>
                        {(ruleViolations[rule.id] ?? []).length === 0 ? (
                          <Typography variant="caption" color="text.secondary">
                            {t("dataQuality.noViolationHistory")}
                          </Typography>
                        ) : (
                          <Table size="small">
                            <TableHead>
                              <TableRow>
                                <TableCell>{t("dataQuality.tableHeaderDetectedAt")}</TableCell>
                                <TableCell>{t("dataQuality.tableHeaderCount")}</TableCell>
                                <TableCell>{t("dataQuality.tableHeaderSampleValues")}</TableCell>
                              </TableRow>
                            </TableHead>
                            <TableBody>
                              {(ruleViolations[rule.id] ?? []).map((v) => (
                                <TableRow key={v.id}>
                                  <TableCell>{new Date(v.detected_at).toLocaleString()}</TableCell>
                                  <TableCell>{v.violation_count}</TableCell>
                                  <TableCell>
                                    {v.sample_values?.values
                                      ? (v.sample_values.values as string[]).join(", ")
                                      : t("common.separator")}
                                  </TableCell>
                                </TableRow>
                              ))}
                            </TableBody>
                          </Table>
                        )}
                      </Box>
                      <Divider />
                    </TableCell>
                  </TableRow>
                )}
              </>
            ))}
          </TableBody>
        </Table>
      )}

      {/* Create rule dialog */}
      <Dialog open={createDialogOpen} onClose={() => setCreateDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("dataQuality.createDialogTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} pt={1}>
            <TextField
              label={t("dataQuality.formLabelName")}
              size="small"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              fullWidth
            />

            <FormControl size="small" fullWidth>
              <InputLabel>{t("dataQuality.formLabelTargetType")}</InputLabel>
              <Select
                value={form.target_type}
                label={t("dataQuality.formLabelTargetType")}
                onChange={(e) =>
                  setForm((f) => ({ ...f, target_type: e.target.value as DataQualityTargetType }))
                }
              >
                {TARGET_TYPES.map((type) => (
                  <MenuItem key={type} value={type}>{getTargetTypeLabel(type, t)}</MenuItem>
                ))}
              </Select>
            </FormControl>

            <TextField
              label={t("dataQuality.formLabelTargetId")}
              size="small"
              value={form.target_id}
              onChange={(e) => setForm((f) => ({ ...f, target_id: e.target.value }))}
              fullWidth
              placeholder={t("dataQuality.formPlaceholderTargetId")}
            />

            <FormControl size="small" fullWidth>
              <InputLabel>{t("dataQuality.formLabelRuleType")}</InputLabel>
              <Select
                value={form.rule_type}
                label={t("dataQuality.formLabelRuleType")}
                onChange={(e) =>
                  setForm((f) => ({ ...f, rule_type: e.target.value as DataQualityRuleType }))
                }
              >
                {RULE_TYPES.map((type) => (
                  <MenuItem key={type} value={type}>{getRuleTypeLabel(type, t)}</MenuItem>
                ))}
              </Select>
            </FormControl>

            {form.rule_type === "range" && (
              <Stack direction="row" spacing={1}>
                <TextField
                  label={t("dataQuality.formLabelMin")}
                  size="small"
                  type="number"
                  value={form.range_min}
                  onChange={(e) => setForm((f) => ({ ...f, range_min: e.target.value }))}
                  sx={{ flex: 1 }}
                />
                <TextField
                  label={t("dataQuality.formLabelMax")}
                  size="small"
                  type="number"
                  value={form.range_max}
                  onChange={(e) => setForm((f) => ({ ...f, range_max: e.target.value }))}
                  sx={{ flex: 1 }}
                />
              </Stack>
            )}

            {form.rule_type === "regex" && (
              <TextField
                label={t("dataQuality.formLabelRegexPattern")}
                size="small"
                value={form.regex_pattern}
                onChange={(e) => setForm((f) => ({ ...f, regex_pattern: e.target.value }))}
                fullWidth
                placeholder={t("dataQuality.formPlaceholderRegexPattern")}
              />
            )}

            {form.rule_type === "custom_sql" && (
              <TextField
                label={t("dataQuality.formLabelCustomSql")}
                size="small"
                multiline
                rows={3}
                value={form.custom_sql}
                onChange={(e) => setForm((f) => ({ ...f, custom_sql: e.target.value }))}
                fullWidth
                placeholder={t("dataQuality.formPlaceholderCustomSql")}
              />
            )}

            <FormControl size="small" fullWidth>
              <InputLabel>{t("dataQuality.formLabelSeverity")}</InputLabel>
              <Select
                value={form.severity}
                label={t("dataQuality.formLabelSeverity")}
                onChange={(e) =>
                  setForm((f) => ({ ...f, severity: e.target.value as DataQualitySeverity }))
                }
              >
                {SEVERITIES.map((severity) => (
                  <MenuItem key={severity} value={severity}>{getSeverityLabel(severity, t)}</MenuItem>
                ))}
              </Select>
            </FormControl>

            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={form.block_on_failure}
                  onChange={(e) => setForm((f) => ({ ...f, block_on_failure: e.target.checked }))}
                />
              }
              label={
                <Typography variant="body2">
                  {t("dataQuality.formLabelBlockOnFailure")}
                </Typography>
              }
            />
          </Stack>

          {createRule.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t("dataQuality.createRuleError")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => createRule.mutate()}
            disabled={!form.name || !form.target_id || createRule.isPending}
          >
            {createRule.isPending ? <CircularProgress size={18} /> : t("dataQuality.createButton")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
