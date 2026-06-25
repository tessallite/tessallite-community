import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  InputAdornment,
  Stack,
  Switch,
  TextField,
  Typography,
} from "@mui/material";
import ClearIcon from "@mui/icons-material/Clear";
import api from "../../api/client";
import CronScheduleField from "./CronScheduleField";

type ModelSettingItem = {
  key: string;
  section: string;
  type: string;
  description: string;
  own_value: unknown;
  effective_value: unknown;
  label?: string | null;
  ui_help?: string | null;
  ui_group?: string | null;
  ui_control?: string | null;
  ui_choices?: unknown[] | null;
  unit?: string | null;
};

type ModelSettingsListResponse = {
  model_id: string;
  items: ModelSettingItem[];
};

const modelSettingsApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<ModelSettingsListResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/models/${encodeURIComponent(modelId)}/settings`,
      )
      .then((r) => r.data),
  put: (projectId: string, modelId: string, key: string, value: unknown) =>
    api
      .put(
        `/api/v1/projects/${encodeURIComponent(projectId)}/models/${encodeURIComponent(modelId)}/settings/${encodeURIComponent(key)}`,
        { value },
      )
      .then((r) => r.data),
};

export default function ModelConfigurationPanel({
  projectId,
  modelId,
  group,
}: {
  projectId: string;
  modelId: string;
  group: string;
}) {
  const t = useT();
  const qc = useQueryClient();

  const settings = useQuery({
    queryKey: ["model-settings", projectId, modelId],
    queryFn: () => modelSettingsApi.list(projectId, modelId),
    enabled: Boolean(projectId && modelId),
  });

  const writeMutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) =>
      modelSettingsApi.put(projectId, modelId, key, value),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["model-settings", projectId, modelId],
      });
    },
  });

  const visible = useMemo(() => {
    const items = (settings.data?.items ?? []).filter(
      (it) => (it.ui_group ?? it.section) === group,
    );
    items.sort((a, b) => (a.label ?? a.key).localeCompare(b.label ?? b.key));
    return items;
  }, [settings.data, group]);

  if (!projectId || !modelId) {
    return (
      <Alert severity="info" sx={{ m: 2 }}>
        {t("modelConfig.openModelToView")}
      </Alert>
    );
  }
  if (settings.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }
  if (settings.error) {
    return (
      <Alert severity="error" sx={{ m: 2 }}>
        {t("modelConfig.failedToLoad", { message: (settings.error as Error).message })}
      </Alert>
    );
  }

  return (
    <Box>
      <Alert severity="info" sx={{ mb: 1.5, fontSize: 12 }}>
        {t("modelConfig.modelSettingsInfo")}
      </Alert>

      {visible.length === 0 ? (
        <Typography color="text.secondary">{t("modelConfig.noSettingsInGroup")}</Typography>
      ) : (
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" },
            gap: 2,
          }}
        >
          {visible.map((item) => (
            <Box
              key={item.key}
              sx={{
                p: 1.5,
                border: 1,
                borderColor: "divider",
                borderRadius: 1,
              }}
            >
              <ModelOverrideRow
                item={item}
                saving={writeMutation.isPending}
                onSave={(value) =>
                  writeMutation.mutate({ key: item.key, value })
                }
                onClear={() =>
                  writeMutation.mutate({ key: item.key, value: null })
                }
              />
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
}

function ModelOverrideRow({
  item,
  saving,
  onSave,
  onClear,
}: {
  item: ModelSettingItem;
  saving: boolean;
  onSave: (value: unknown) => void;
  onClear: () => void;
}) {
  const t = useT();
  const inheritedDisplay = stringify(item.effective_value);
  const hasOwn = item.own_value !== null && item.own_value !== undefined;
  const initial = hasOwn ? stringify(item.own_value) : "";
  const [draft, setDraft] = useState<string>(initial);
  const [boolDraft, setBoolDraft] = useState<boolean>(
    hasOwn ? Boolean(item.own_value) : Boolean(item.effective_value),
  );
  const [error, setError] = useState<string | null>(null);

  const friendlyLabel = item.label ?? prettifyKey(item.key);
  const description = (item.ui_help ?? item.description ?? "").trim();
  const control = item.ui_control ?? defaultControl(item.type);
  const dirty =
    control === "switch"
      ? boolDraft !== (hasOwn ? Boolean(item.own_value) : Boolean(item.effective_value))
      : draft !== initial;

  function handleSave() {
    setError(null);
    if (control === "switch") {
      onSave(boolDraft);
      return;
    }
    if (draft.trim() === "") {
      onClear();
      return;
    }
    try {
      onSave(parseValue(item.type, draft));
    } catch (e) {
      setError(t((e as Error).message));
    }
  }

  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap">
        <Typography variant="body2" sx={{ fontWeight: 600 }}>
          {friendlyLabel}
        </Typography>
        {hasOwn && (
          <Typography variant="caption" sx={{ color: "primary.main", fontWeight: 600, fontSize: 11 }}>
            {t("modelConfig.overridden")}
          </Typography>
        )}
      </Stack>
      {description && (
        <Typography variant="caption" color="text.secondary">
          {description}
        </Typography>
      )}
      <Typography variant="caption" color="text.disabled" sx={{ fontSize: 11 }}>
        {t("modelConfig.inherits")} <code>{inheritedDisplay || t("modelConfig.unset")}</code>
        {item.unit ? ` · ${item.unit}` : ""}
      </Typography>

      <Box>
        {control === "switch" ? (
          <Stack direction="row" spacing={1} alignItems="center">
            <Switch
              checked={boolDraft}
              onChange={(e) => setBoolDraft(e.target.checked)}
              disabled={saving}
            />
            <Typography variant="body2" color="text.secondary">
              {boolDraft ? t("modelConfig.enabled") : t("modelConfig.disabled")}
            </Typography>
          </Stack>
        ) : control === "cron" ? (
          <CronScheduleField
            value={draft || inheritedDisplay}
            onChange={setDraft}
            disabled={saving}
            error={error}
          />
        ) : (
          <TextField
            size="small"
            fullWidth
            type={control === "number" || control === "slider" ? "number" : "text"}
            placeholder={inheritedDisplay || t("modelConfig.noInheritedValue")}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={saving}
            error={Boolean(error)}
            helperText={error || (hasOwn ? t("modelConfig.overridesProject") : t("modelConfig.blankInherit"))}
            InputProps={
              item.unit
                ? { endAdornment: <InputAdornment position="end">{item.unit}</InputAdornment> }
                : undefined
            }
          />
        )}
      </Box>

      <Stack direction="row" spacing={0.5} justifyContent="flex-end">
        {hasOwn && (
          <Button
            size="small"
            variant="outlined"
            onClick={onClear}
            disabled={saving}
            startIcon={<ClearIcon />}
          >
            {t("modelConfig.clear")}
          </Button>
        )}
        <Button
          size="small"
          variant="contained"
          onClick={handleSave}
          disabled={!dirty || saving}
        >
          {saving ? <CircularProgress size={14} /> : t("modelConfig.save")}
        </Button>
      </Stack>
    </Stack>
  );
}

function defaultControl(type: string): string {
  if (type === "bool") return "switch";
  if (type === "cron") return "cron";
  if (type === "int" || type === "float") return "number";
  return "text";
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function prettifyKey(key: string): string {
  return key
    .split(".")
    .pop()!
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function parseValue(type: string, raw: string): unknown {
  if (type === "int") {
    const n = Number(raw);
    if (!Number.isFinite(n) || !Number.isInteger(n)) {
      throw new Error("modelConfig.mustBeInteger");
    }
    return n;
  }
  if (type === "float") {
    const n = Number(raw);
    if (!Number.isFinite(n)) throw new Error("modelConfig.mustBeNumber");
    return n;
  }
  if (type === "bool") return raw.trim().toLowerCase() === "true";
  if (type === "list[str]") {
    return raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  if (type === "dict") {
    try {
      return JSON.parse(raw);
    } catch {
      throw new Error("modelConfig.invalidJson");
    }
  }
  return raw;
}
