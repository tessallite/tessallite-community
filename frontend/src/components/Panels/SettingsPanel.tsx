/**
 * Settings Panel — flattened tab structure for model builder preferences
 * and per-model configuration overrides.
 *
 * Preferences: endpoint URLs, theme, canvas notation/pathing.
 * Model config tabs: Aggregates, AI Optimizer, Pocket, Predictive, Limits.
 */
import { useState } from "react";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { useT } from "../../i18n";
import { useCanAuthorModel } from "../../auth/useCanAuthorModel";
import {
  Alert,
  Box,
  Button,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Radio,
  RadioGroup,
  Select,
  Stack,
  Tab,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";
import { getSystemDefaults } from "../../api/systemDefaults";
import ModelConfigurationPanel from "../Settings/ModelConfigurationPanel";
import ModelLLMFunctions from "../Settings/ModelLLMFunctions";
import { SLAConfigPanel } from "../Settings/SLAConfigPanel";
import { SolidatusIntegrationPanel } from "./SolidatusIntegrationPanel";
import { CollibraIntegrationPanel } from "./CollibraIntegrationPanel";
import {
  useBuilderStore,
  type RelationNotation,
  type RelationPathing,
} from "../../store/builderStore";

const KEY_QUERY_ROUTER = "builder.settings.queryRouterUrl";
const KEY_GATEWAY_HTTP = "builder.settings.gatewayHttpUrl";
const KEY_GATEWAY_JDBC_HOST = "builder.settings.gatewayJdbcHost";
const KEY_GATEWAY_JDBC_PORT = "builder.settings.gatewayJdbcPort";
const KEY_THEME = "builder.settings.theme";
const SETTINGS_CHANGED_EVENT = "builder-settings-changed";

function defaultHost() {
  return typeof window !== "undefined" ? window.location.hostname || "localhost" : "localhost";
}
function defaultProtocol() {
  return typeof window !== "undefined" ? window.location.protocol || "http:" : "http:";
}
function notifySettingsChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(SETTINGS_CHANGED_EVENT));
}

type SettingsTab =
  | "preferences"
  | "llm"
  | "aggregates"
  | "ai-optimizer"
  | "pocket"
  | "predictive"
  | "limits"
  | "sla"
  | "statistics"
  | "solidatus"
  | "collibra";

const TAB_LABELS_KEYS: Record<SettingsTab, string> = {
  preferences: "settings.tabs.preferences",
  llm: "settings.tabs.llm",
  aggregates: "settings.tabs.aggregates",
  "ai-optimizer": "settings.tabs.aiOptimizer",
  pocket: "settings.tabs.pocket",
  predictive: "settings.tabs.predictive",
  limits: "settings.tabs.limits",
  sla: "settings.tabs.sla",
  statistics: "settings.tabs.statistics",
  solidatus: "settings.tabs.solidatus",
  collibra: "settings.tabs.collibra",
};

const CONFIG_GROUP_MAP: Partial<Record<SettingsTab, string>> = {
  aggregates: "Aggregates",
  "ai-optimizer": "AI optimizer",
  pocket: "Pocket tables",
  predictive: "Predictive aggregates",
  limits: "Per-model limits",
};

type SettingsPanelProps = {
  projectId?: string;
  modelId?: string;
};

export default function SettingsPanel({ projectId, modelId }: SettingsPanelProps = {}) {
  const t = useT();
  const [tab, setTab] = useState<SettingsTab>("preferences");
  const showModelTabs = Boolean(projectId && modelId);
  // Bug-8170: Collibra / Solidatus panels expose admin/modeler-only mutations
  // (create, activate/deactivate, edit, delete, sync) with no client-side gate
  // at all — a viewer could open either tab and be offered controls the
  // backend rejects (api/collibra.py, api/solidatus.py: require_role("admin")
  // for create/update/delete, require_role("modeler") for validate/sync). This
  // is a UI gate only, defense-in-depth — the backend role checks above stay
  // authoritative regardless of this flag.
  //
  // Round-2 fix: gate on the server-derived per-model authoring authority
  // (`useCanAuthorModel()`, backed by `caller_can_author` / builder-store
  // `readOnly` — the same source every other Model Builder authoring panel
  // uses), NOT the coarse local role via `canEditModelConfig()`. The
  // canonical locally-provisioned modeller is local role `member` plus a
  // per-project `modeler` binding — `modeler` is never a `LocalUser.role`
  // value — so the coarse check wrongly hid both tabs from an authorized
  // modeller (Bug-8747 / questions_modeller-authoring-authority-source.md,
  // decided: fail closed on the per-model signal, no legacy role fallback).
  const canManageIntegrations = useCanAuthorModel();
  const allTabs: SettingsTab[] = showModelTabs
    ? [
        "preferences", "llm", "aggregates", "ai-optimizer", "pocket", "predictive", "limits", "sla", "statistics",
        ...(canManageIntegrations ? (["solidatus", "collibra"] as const) : []),
      ]
    : ["preferences"];
  const activeTab = allTabs.includes(tab) ? tab : "preferences";

  return (
    <Box>
      <Tabs
        value={activeTab}
        onChange={(_, v) => setTab(v as SettingsTab)}
        variant="scrollable"
        scrollButtons="auto"
        allowScrollButtonsMobile
        sx={{
          borderBottom: 1,
          borderColor: "divider",
          mb: 1.5,
          minHeight: 36,
          // Keep the scroll arrows visible (not collapsed to zero width) when
          // tabs overflow, so the user can see more tabs exist (Bug-5332).
          "& .MuiTabs-scrollButtons.Mui-disabled": { opacity: 0.3 },
        }}
      >
        {allTabs.map((k) => (
          <Tab
            key={k}
            value={k}
            label={t(TAB_LABELS_KEYS[k])}
            sx={{ minHeight: 36, py: 0, textTransform: "none", fontSize: 13 }}
          />
        ))}
      </Tabs>

      {activeTab === "preferences" && <PreferencesTab />}
      {activeTab === "llm" && showModelTabs && (
        <ModelLLMFunctions projectId={projectId!} modelId={modelId!} />
      )}
      {activeTab === "sla" && showModelTabs && (
        <SLAConfigPanel projectId={projectId!} modelId={modelId!} />
      )}
      {activeTab === "statistics" && showModelTabs && <StatisticsSettingsTab />}
      {activeTab === "solidatus" && showModelTabs && (
        <SolidatusIntegrationPanel />
      )}
      {activeTab === "collibra" && showModelTabs && (
        <CollibraIntegrationPanel />
      )}
      {activeTab !== "preferences" && activeTab !== "llm" && activeTab !== "sla" && activeTab !== "statistics" && activeTab !== "solidatus" && activeTab !== "collibra" && showModelTabs && (
        <ModelConfigurationPanel
          projectId={projectId!}
          modelId={modelId!}
          group={CONFIG_GROUP_MAP[activeTab]!}
        />
      )}
    </Box>
  );
}

function PreferencesTab() {
  const t = useT();
  const host = defaultHost();
  const protocol = defaultProtocol();
  const ports = getSystemDefaults().endpointDefaults;

  const [queryRouterUrl, setQueryRouterUrl] = useState(
    safeLocalGet(KEY_QUERY_ROUTER, `${protocol}//${host}:${ports.model_service_port}`),
  );
  const [gatewayHttpUrl, setGatewayHttpUrl] = useState(
    safeLocalGet(KEY_GATEWAY_HTTP, `${protocol}//${host}:${ports.gateway_http_port}`),
  );
  const [gatewayJdbcHost, setGatewayJdbcHost] = useState(
    safeLocalGet(KEY_GATEWAY_JDBC_HOST, host),
  );
  const [gatewayJdbcPort, setGatewayJdbcPort] = useState(
    safeLocalGet(KEY_GATEWAY_JDBC_PORT, String(ports.gateway_jdbc_port)),
  );
  const [theme, setTheme] = useState(safeLocalGet(KEY_THEME, "light"));
  const [saved, setSaved] = useState(false);

  const relationNotation = useBuilderStore((s) => s.relationNotation);
  const setRelationNotation = useBuilderStore((s) => s.setRelationNotation);
  const relationPathing = useBuilderStore((s) => s.relationPathing);
  const setRelationPathing = useBuilderStore((s) => s.setRelationPathing);

  function saveSettings() {
    localStorage.setItem(KEY_QUERY_ROUTER, queryRouterUrl.trim());
    localStorage.setItem(KEY_GATEWAY_HTTP, gatewayHttpUrl.trim());
    localStorage.setItem(KEY_GATEWAY_JDBC_HOST, gatewayJdbcHost.trim());
    localStorage.setItem(KEY_GATEWAY_JDBC_PORT, gatewayJdbcPort.trim());
    localStorage.setItem(KEY_THEME, theme);
    notifySettingsChanged();
    setSaved(true);
    setTimeout(() => setSaved(false), 1800);
  }

  function resetDefaults() {
    localStorage.removeItem(KEY_QUERY_ROUTER);
    localStorage.removeItem(KEY_GATEWAY_HTTP);
    localStorage.removeItem(KEY_GATEWAY_JDBC_HOST);
    localStorage.removeItem(KEY_GATEWAY_JDBC_PORT);
    localStorage.removeItem(KEY_THEME);
    setQueryRouterUrl(`${protocol}//${host}:${ports.model_service_port}`);
    setGatewayHttpUrl(`${protocol}//${host}:${ports.gateway_http_port}`);
    setGatewayJdbcHost(host);
    setGatewayJdbcPort(String(ports.gateway_jdbc_port));
    setTheme("light");
    notifySettingsChanged();
    setSaved(false);
  }

  return (
    <Stack spacing={2.5}>
      <Box>
        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
          {t("settings.endpoints")}
        </Typography>
        <Stack spacing={1.5}>
          <TextField
            label={t("settings.queryRouterUrl")}
            size="small"
            value={queryRouterUrl}
            onChange={(e) => setQueryRouterUrl(e.target.value)}
            fullWidth
          />
          <TextField
            label={t("settings.gatewayHttpUrl")}
            size="small"
            value={gatewayHttpUrl}
            onChange={(e) => setGatewayHttpUrl(e.target.value)}
            fullWidth
          />
          <Stack direction="row" spacing={1}>
            <TextField
              label={t("settings.gatewayJdbcHost")}
              size="small"
              value={gatewayJdbcHost}
              onChange={(e) => setGatewayJdbcHost(e.target.value)}
              sx={{ flex: 1 }}
            />
            <TextField
              label={t("settings.jdbcPort")}
              size="small"
              value={gatewayJdbcPort}
              onChange={(e) => setGatewayJdbcPort(e.target.value)}
              sx={{ width: 100 }}
            />
          </Stack>
        </Stack>
      </Box>

      <Box>
        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
          {t("settings.appearance")}
        </Typography>
        <FormControl size="small" fullWidth>
          <InputLabel>{t("settings.theme")}</InputLabel>
          <Select
            value={theme}
            label={t("settings.theme")}
            onChange={(e) => setTheme(String(e.target.value))}
          >
            <MenuItem value="light">{t("settings.themeLight")}</MenuItem>
            <MenuItem value="dark">{t("settings.themeDark")}</MenuItem>
          </Select>
        </FormControl>
      </Box>

      <Box>
        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
          {t("settings.canvasLayout")}
        </Typography>
        <Stack spacing={2}>
          <FormControl>
            <Typography variant="caption" color="text.secondary" sx={{ mb: 0.5 }}>
              {t("settings.notationStyle")}
            </Typography>
            <RadioGroup
              value={relationNotation}
              onChange={(e) => setRelationNotation(e.target.value as RelationNotation)}
            >
              <FormControlLabel value="crowsfoot" control={<Radio size="small" />} label={t("settings.crowsFoot")} />
              <FormControlLabel value="uml" control={<Radio size="small" />} label={t("settings.umlArrow")} />
              <FormControlLabel value="diamond" control={<Radio size="small" />} label={t("settings.diamond")} />
            </RadioGroup>
          </FormControl>
          <FormControl>
            <Typography variant="caption" color="text.secondary" sx={{ mb: 0.5 }}>
              {t("settings.edgePathing")}
            </Typography>
            <RadioGroup
              value={relationPathing}
              onChange={(e) => setRelationPathing(e.target.value as RelationPathing)}
            >
              <FormControlLabel value="orthogonal" control={<Radio size="small" />} label={t("settings.orthogonal")} />
              <FormControlLabel value="straight" control={<Radio size="small" />} label={t("settings.straight")} />
            </RadioGroup>
          </FormControl>
        </Stack>
      </Box>

      {saved && <Alert severity="success">{t("settings.settingsSaved")}</Alert>}
      <Stack direction="row" spacing={1}>
        <Button variant="contained" size="small" onClick={saveSettings}>
          {t("common.save")}
        </Button>
        <Button variant="outlined" size="small" onClick={resetDefaults}>
          {t("settings.resetDefaults")}
        </Button>
      </Stack>
    </Stack>
  );
}

const KEY_LOW_CARDINALITY = "builder.settings.lowCardinalityThreshold";

function StatisticsSettingsTab() {
  const t = useT();
  const [threshold, setThreshold] = useState(
    safeLocalGet(KEY_LOW_CARDINALITY, "50"),
  );
  const [saved, setSaved] = useState(false);

  function save() {
    const parsed = parseInt(threshold, 10);
    const clamped = isNaN(parsed) ? 50 : Math.max(1, Math.min(1000, parsed));
    localStorage.setItem(KEY_LOW_CARDINALITY, String(clamped));
    setThreshold(String(clamped));
    setSaved(true);
    setTimeout(() => setSaved(false), 1800);
  }

  function reset() {
    localStorage.removeItem(KEY_LOW_CARDINALITY);
    setThreshold("50");
    setSaved(false);
  }

  return (
    <Stack spacing={2.5}>
      <Box>
        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 0.5 }}>
          {t("settings.lowCardinalityThreshold")}
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
          {t("settings.lowCardinalityHelp")}
        </Typography>
        <TextField
          size="small"
          type="number"
          value={threshold}
          onChange={(e) => setThreshold(e.target.value)}
          inputProps={{ min: 1, max: 1000 }}
          sx={{ width: 140 }}
          label={t("settings.threshold")}
        />
      </Box>
      {saved && <Alert severity="success">{t("settings.statisticsSaved")}</Alert>}
      <Stack direction="row" spacing={1}>
        <Button variant="contained" size="small" onClick={save}>
          {t("common.save")}
        </Button>
        <Button variant="outlined" size="small" onClick={reset}>
          {t("settings.resetDefault")}
        </Button>
      </Stack>
    </Stack>
  );
}
