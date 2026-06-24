import { useCallback, useState } from "react";
import { useT } from "../../i18n";
import { useQueryClient } from "@tanstack/react-query";
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
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import { llmConfigsApi } from "../../api/client";
import { defaultModelNameFor } from "../../api/systemDefaults";
import { useConfirm } from "../Confirm";
import { useLLMConfigs } from "../../api/hooks";
import type {
  LLMProviderConfig,
  LLMProviderConfigCreate,
  LLMProviderConfigUpdate,
  LLMConnectionTestResponse,
} from "../../api/types";

const PROVIDERS = [
  { value: "openai", label: "llmProvider.openai" },
  { value: "google", label: "llmProvider.google" },
  { value: "anthropic", label: "llmProvider.anthropic" },
  { value: "deepseek", label: "llmProvider.deepseek" },
  { value: "glm", label: "llmProvider.glm" },
  { value: "ollama", label: "llmProvider.ollama" },
];

const NEEDS_BASE_URL = new Set(["deepseek", "glm", "ollama"]);

interface ConfigFormState {
  provider: string;
  display_name: string;
  base_url: string;
  api_key: string;
  model_name: string;
  max_tokens: number;
  temperature: number;
  timeout_seconds: number;
  anthropic_api_version: string;
  google_mode: string;
  google_project: string;
  google_location: string;
}

const EMPTY_FORM: ConfigFormState = {
  provider: "openai",
  display_name: "",
  base_url: "",
  api_key: "",
  model_name: "",
  max_tokens: 16384,
  temperature: 0.2,
  timeout_seconds: 120,
  anthropic_api_version: "",
  google_mode: "",
  google_project: "",
  google_location: "",
};

/**
 * Project-scoped LLM Provider Config CRUD.
 *
 * Renders a table of LLMProviderConfig rows for the given project with
 * Add / Edit / Delete / Test actions. The Anthropic API version field is
 * shown only when provider == "anthropic" and is stored in the per-row
 * ``config`` JSONB.
 */
export default function LLMConfigurationsPanel({ projectId }: { projectId: string }) {
  const t = useT();
  const qc = useQueryClient();
  const { data: configs, isLoading } = useLLMConfigs(projectId);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [editHasKey, setEditHasKey] = useState(false);
  const [form, setForm] = useState<ConfigFormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<LLMConnectionTestResponse | null>(null);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const openCreate = useCallback(() => {
    setEditId(null);
    setEditHasKey(false);
    setForm(EMPTY_FORM);
    setTestResult(null);
    setError(null);
    setDialogOpen(true);
  }, []);

  const openEdit = useCallback((cfg: LLMProviderConfig) => {
    setEditId(cfg.id);
    setEditHasKey(cfg.has_api_key);
    setForm({
      provider: cfg.provider,
      display_name: cfg.display_name,
      base_url: cfg.base_url ?? "",
      api_key: "",
      model_name: cfg.model_name,
      max_tokens: cfg.max_tokens,
      temperature: cfg.temperature,
      timeout_seconds: cfg.timeout_seconds,
      anthropic_api_version: String(
        (cfg.config?.anthropic_api_version ?? "") as string,
      ),
      google_mode: String((cfg.config?.google_mode ?? "") as string),
      google_project: String((cfg.config?.google_project ?? "") as string),
      google_location: String((cfg.config?.google_location ?? "") as string),
    });
    setTestResult(null);
    setError(null);
    setDialogOpen(true);
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      const config: Record<string, unknown> = {};
      if (form.provider === "anthropic" && form.anthropic_api_version) {
        config.anthropic_api_version = form.anthropic_api_version;
      }
      if (form.provider === "google") {
        if (form.google_mode) config.google_mode = form.google_mode;
        if (form.google_project) config.google_project = form.google_project;
        if (form.google_location) config.google_location = form.google_location;
      }
      if (editId) {
        const update: LLMProviderConfigUpdate = {
          provider: form.provider,
          display_name: form.display_name,
          model_name: form.model_name,
          max_tokens: form.max_tokens,
          temperature: form.temperature,
          timeout_seconds: form.timeout_seconds,
          config,
        };
        if (form.base_url) update.base_url = form.base_url;
        if (form.api_key) update.api_key = form.api_key;
        await llmConfigsApi.update(projectId, editId, update);
      } else {
        const create: LLMProviderConfigCreate = {
          provider: form.provider,
          display_name: form.display_name,
          api_key: form.api_key,
          model_name: form.model_name,
          max_tokens: form.max_tokens,
          temperature: form.temperature,
          timeout_seconds: form.timeout_seconds,
          config,
        };
        if (form.base_url) create.base_url = form.base_url;
        await llmConfigsApi.create(projectId, create);
      }
      qc.invalidateQueries({ queryKey: ["llmConfigs", projectId] });
      setDialogOpen(false);
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("common.saveFailed");
      setError(detail);
    } finally {
      setSaving(false);
    }
  }, [editId, form, qc, projectId]);

  const confirm = useConfirm();
  const handleDelete = useCallback(
    async (id: string, name?: string) => {
      const ok = await confirm({
        title: t("llm.deleteTitle"),
        message: t("llm.deleteConfirmMessage", { name: name ? ` the ${name}` : "" }),
        confirmLabel: t("llm.deleteConfirm"),
      });
      if (!ok) return;
      setDeleting(id);
      try {
        await llmConfigsApi.delete(projectId, id);
        qc.invalidateQueries({ queryKey: ["llmConfigs", projectId] });
      } finally {
        setDeleting(null);
      }
    },
    [confirm, qc, projectId],
  );

  const handleTest = useCallback(async () => {
    setTesting(true);
    setTestResult(null);
    try {
      let result: LLMConnectionTestResponse;
      if (editId) {
        result = await llmConfigsApi.test(projectId, editId);
      } else {
        const cfg: Record<string, unknown> = {};
        if (form.provider === "google") {
          if (form.google_mode) cfg.google_mode = form.google_mode;
          if (form.google_project) cfg.google_project = form.google_project;
          if (form.google_location) cfg.google_location = form.google_location;
        }
        result = await llmConfigsApi.testAdhoc(projectId, {
          provider: form.provider,
          base_url: form.base_url || undefined,
          api_key: form.api_key,
          model_name: form.model_name,
          max_tokens: form.max_tokens,
          temperature: form.temperature,
          timeout_seconds: form.timeout_seconds,
          config: cfg,
        });
      }
      setTestResult(result);
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("llm.testFailed");
      setTestResult({ success: false, message: detail, latency_ms: null });
    } finally {
      setTesting(false);
    }
  }, [editId, form, projectId]);

  const [listTestId, setListTestId] = useState<string | null>(null);
  const [listTestResult, setListTestResult] = useState<{ id: string; result: LLMConnectionTestResponse } | null>(null);
  const handleListTest = useCallback(async (configId: string) => {
    setListTestId(configId);
    setListTestResult(null);
    try {
      const result = await llmConfigsApi.test(projectId, configId);
      setListTestResult({ id: configId, result });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("llm.testFailed");
      setListTestResult({ id: configId, result: { success: false, message: detail, latency_ms: null } });
    } finally {
      setListTestId(null);
    }
  }, [projectId]);

  if (isLoading) return <CircularProgress size={24} />;

  return (
    <Box>
      <Box display="flex" alignItems="center" mb={1}>
        <Typography variant="subtitle2" fontWeight={600} flexGrow={1}>
          {t("llm.panelTitle")}
        </Typography>
        <Button size="small" variant="contained" startIcon={<AddIcon />} onClick={openCreate}>
          {t("llm.addConfig")}
        </Button>
      </Box>

      <TableContainer>
        <Table size="small">
          <TableHead>
            <TableRow sx={{ bgcolor: "grey.50" }}>
              <TableCell>{t("llm.nameHeader")}</TableCell>
              <TableCell>{t("llm.providerHeader")}</TableCell>
              <TableCell>{t("llm.modelHeader")}</TableCell>
              <TableCell align="right">{t("llm.maxTokensHeader")}</TableCell>
              <TableCell align="right">{t("llm.temperatureHeader")}</TableCell>
              <TableCell align="right">{t("llm.timeoutHeader")}</TableCell>
              <TableCell>{t("llm.apiKeyHeader")}</TableCell>
              <TableCell align="right">{t("llm.actionsHeader")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {(configs ?? []).map((cfg: LLMProviderConfig) => (
              <TableRow key={cfg.id}>
                <TableCell>{cfg.display_name}</TableCell>
                <TableCell>
                  <Chip label={cfg.provider} size="small" variant="outlined" />
                </TableCell>
                <TableCell sx={{ fontFamily: "monospace", fontSize: 12 }}>
                  {cfg.model_name}
                </TableCell>
                <TableCell align="right" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                  {cfg.max_tokens.toLocaleString()}
                </TableCell>
                <TableCell align="right" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                  {cfg.temperature}
                </TableCell>
                <TableCell align="right" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                  {cfg.timeout_seconds}s
                </TableCell>
                <TableCell>
                  <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                    {cfg.has_api_key ? "•••••••" : "—"}
                  </Typography>
                </TableCell>
                <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                  <Tooltip title={t("llm.testConnectionTooltip")}>
                    <IconButton
                      size="small"
                      onClick={() => handleListTest(cfg.id)}
                      disabled={listTestId === cfg.id}
                    >
                      {listTestId === cfg.id ? (
                        <CircularProgress size={16} />
                      ) : (
                        <PlayArrowIcon fontSize="small" />
                      )}
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("llm.editTooltip")}>
                    <IconButton size="small" onClick={() => openEdit(cfg)}>
                      <EditIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("llm.deleteTooltip")}>
                    <IconButton
                      size="small"
                      onClick={() => handleDelete(cfg.id, cfg.display_name)}
                      disabled={deleting === cfg.id}
                    >
                      {deleting === cfg.id ? (
                        <CircularProgress size={16} />
                      ) : (
                        <DeleteIcon fontSize="small" />
                      )}
                    </IconButton>
                  </Tooltip>
                  {listTestResult?.id === cfg.id && (
                    <Chip
                      size="small"
                      label={
                        listTestResult.result.success
                          ? `${t("llm.testResultPrefix")}${listTestResult.result.latency_ms != null ? ` (${listTestResult.result.latency_ms.toFixed(0)} ms)` : ""}`
                          : listTestResult.result.message.slice(0, 40)
                      }
                      color={listTestResult.result.success ? "success" : "error"}
                      variant="outlined"
                      sx={{ ml: 0.5 }}
                      onDelete={() => setListTestResult(null)}
                    />
                  )}
                </TableCell>
              </TableRow>
            ))}
            {(configs ?? []).length === 0 && (
              <TableRow>
                <TableCell colSpan={8}>
                  <Typography variant="body2" color="text.secondary" textAlign="center">
                    {t("llm.noConfigs")}
                  </Typography>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>

      <Dialog open={dialogOpen} onClose={() => setDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{editId ? t("llm.editTitle") : t("llm.addTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            {error && <Alert severity="error">{error}</Alert>}

            <TextField
              label={t("llm.displayNameLabel")}
              size="small"
              value={form.display_name}
              onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))}
              fullWidth
            />

            <FormControl size="small" fullWidth>
              <InputLabel>{t("llm.providerLabel")}</InputLabel>
              <Select
                value={form.provider}
                label={t("llm.providerLabel")}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    provider: e.target.value,
                    model_name: defaultModelNameFor(e.target.value),
                  }))
                }
              >
                {PROVIDERS.map((p) => (
                  <MenuItem key={p.value} value={p.value}>
                    {t(p.label)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            <TextField
              label={t("llm.apiKeyLabel")}
              size="small"
              type="password"
              value={form.api_key}
              onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))}
              placeholder={editId && editHasKey ? "•••••••" : ""}
              helperText={
                editId
                  ? editHasKey
                    ? t("llm.keyIsSet")
                    : t("llm.noKeyStored")
                  : undefined
              }
              fullWidth
            />

            {form.provider === "google" && (
              <>
                <FormControl size="small" fullWidth>
                  <InputLabel>{t("llm.googleModeLabel")}</InputLabel>
                  <Select
                    value={form.google_mode}
                    label={t("llm.googleModeLabel")}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, google_mode: e.target.value }))
                    }
                  >
                    <MenuItem value="">
                      {t("llm.googleModeApiKey")}
                    </MenuItem>
                    <MenuItem value="vertex_ai">
                      {t("llm.googleModeVertexAi")}
                    </MenuItem>
                  </Select>
                </FormControl>
                {form.google_mode === "vertex_ai" && (
                  <Stack direction="row" spacing={1}>
                    <TextField
                      label={t("llm.googleProjectLabel")}
                      size="small"
                      value={form.google_project}
                      onChange={(e) =>
                        setForm((f) => ({ ...f, google_project: e.target.value }))
                      }
                      placeholder="tessallite-io"
                      helperText={t("llm.googleProjectHelp")}
                      sx={{ flex: 2 }}
                    />
                    <TextField
                      label={t("llm.googleLocationLabel")}
                      size="small"
                      value={form.google_location}
                      onChange={(e) =>
                        setForm((f) => ({ ...f, google_location: e.target.value }))
                      }
                      placeholder="global"
                      helperText={t("llm.googleLocationHelp")}
                      sx={{ flex: 1 }}
                    />
                  </Stack>
                )}
              </>
            )}

            {NEEDS_BASE_URL.has(form.provider) && (
              <TextField
                label={t("llm.baseUrlLabel")}
                size="small"
                value={form.base_url}
                onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
                placeholder={form.provider === "ollama" ? t("llm.apiUrlPlaceholder") : ""}
                fullWidth
              />
            )}

            <TextField
              label={t("llm.modelNameLabel")}
              size="small"
              value={form.model_name}
              onChange={(e) => setForm((f) => ({ ...f, model_name: e.target.value }))}
              placeholder={defaultModelNameFor(form.provider)}
              fullWidth
            />

            {form.provider === "anthropic" && (
              <TextField
                label={t("llm.anthropicVersionLabel")}
                size="small"
                value={form.anthropic_api_version}
                onChange={(e) =>
                  setForm((f) => ({ ...f, anthropic_api_version: e.target.value }))
                }
                placeholder={t("llm.cutoffPlaceholder")}
                helperText={t("llm.anthropicVersionHelp")}
                fullWidth
              />
            )}

            <Stack direction="row" spacing={1}>
              <TextField
                label={t("llm.maxTokensLabel")}
                size="small"
                type="number"
                inputProps={{ min: 256, max: 131072, step: 1024 }}
                value={form.max_tokens}
                onChange={(e) => setForm((f) => ({ ...f, max_tokens: Number(e.target.value) }))}
                helperText={t("llm.maxTokensHelp")}
                sx={{ flex: 1 }}
              />
              <TextField
                label={t("llm.temperatureLabel")}
                size="small"
                type="number"
                inputProps={{ step: 0.1, min: 0, max: 2 }}
                value={form.temperature}
                onChange={(e) => setForm((f) => ({ ...f, temperature: Number(e.target.value) }))}
                helperText={t("llm.temperatureHelp")}
                sx={{ flex: 1 }}
              />
              <TextField
                label={t("llm.timeoutLabel")}
                size="small"
                type="number"
                inputProps={{ min: 10, max: 600, step: 10 }}
                value={form.timeout_seconds}
                onChange={(e) =>
                  setForm((f) => ({ ...f, timeout_seconds: Number(e.target.value) }))
                }
                helperText={t("llm.timeoutHelp")}
                sx={{ flex: 1 }}
              />
            </Stack>

            <Box>
              <Button
                variant="outlined"
                size="small"
                onClick={handleTest}
                disabled={testing || (!editId && !form.api_key && !(form.provider === "google" && form.google_mode === "vertex_ai")) || !form.model_name}
              >
                {testing ? (
                  <>
                    <CircularProgress size={14} sx={{ mr: 1 }} /> {t("llm.testing")}
                  </>
                ) : (
                  t("llm.testConnection")
                )}
              </Button>
              {!editId && !form.api_key && !(form.provider === "google" && form.google_mode === "vertex_ai") && (
                <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                  {t("llm.enterKeyToTest")}
                </Typography>
              )}
              {testResult && (
                <Alert severity={testResult.success ? "success" : "error"} sx={{ mt: 1 }}>
                  {testResult.message}
                  {testResult.latency_ms != null && ` (${testResult.latency_ms.toFixed(0)} ms)`}
                </Alert>
              )}
            </Box>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("llm.cancel")}</Button>
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={
              saving ||
              !form.display_name ||
              !form.model_name ||
              (!editId && !form.api_key && !(form.provider === "google" && form.google_mode === "vertex_ai"))
            }
          >
            {saving ? t("llm.saving") : t("llm.save")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
