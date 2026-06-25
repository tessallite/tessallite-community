import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Stack,
  Tab,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";
import { systemSettingsApi } from "../../api/client";
import type { SystemSettingItem, BootstrapItem } from "../../api/client";
import SettingFieldRenderer from "./SettingFieldRenderer";
import HelpIconButton from "../HelpIconButton";

/**
 * System Configuration tab.
 *
 * Tabbed card grid: Auth · Daily schedule · Timeouts · Ceilings · Environment.
 * Each tab shows a compact 2-column card layout, inline edit per field.
 * Environment renders read-only env values for operator visibility.
 */

type TabKey = "auth" | "scheduling" | "timeouts" | "ceilings" | "bootstrap";

const TAB_GROUP_MAP: Record<TabKey, string[]> = {
  auth: ["Security"],
  scheduling: ["Scheduler"],
  timeouts: ["Network timeouts", "Frontend"],
  ceilings: ["Query results", "Rate limiting"],
  bootstrap: [],
};

export default function SystemConfigurationTab() {
  const t = useT();
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabKey>("auth");
  const [filter, setFilter] = useState("");
  const [restartHelpOpen, setRestartHelpOpen] = useState(false);
  const [savingKey, setSavingKey] = useState<string | null>(null);

  const settings = useQuery({
    queryKey: ["system-settings"],
    queryFn: systemSettingsApi.list,
  });
  const restartPending = useQuery({
    queryKey: ["system-settings", "restart-pending"],
    queryFn: systemSettingsApi.restartPending,
  });
  const bootstrap = useQuery({
    queryKey: ["system-settings", "bootstrap"],
    queryFn: systemSettingsApi.bootstrap,
  });

  const writeMutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) =>
      systemSettingsApi.put(key, value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["system-settings"] });
    },
  });

  const itemsByTab = useMemo(() => {
    const all = (settings.data ?? []).filter(
      (it) => it.key !== "meta.bootstrap_env_view",
    );
    const q = filter.trim().toLowerCase();
    const filtered = q
      ? all.filter(
          (it) =>
            (it.label ?? "").toLowerCase().includes(q) ||
            (it.ui_help ?? "").toLowerCase().includes(q) ||
            it.key.toLowerCase().includes(q) ||
            it.description.toLowerCase().includes(q),
        )
      : all;
    const map: Record<TabKey, SystemSettingItem[]> = {
      auth: [],
      scheduling: [],
      timeouts: [],
      ceilings: [],
      bootstrap: [],
    };
    for (const it of filtered) {
      const grp = it.ui_group ?? it.section;
      for (const k of Object.keys(TAB_GROUP_MAP) as TabKey[]) {
        if (TAB_GROUP_MAP[k].includes(grp)) {
          map[k].push(it);
          break;
        }
      }
    }
    for (const k of Object.keys(map) as TabKey[]) {
      map[k].sort((a, b) =>
        (a.label ?? a.key).localeCompare(b.label ?? b.key),
      );
    }
    return map;
  }, [settings.data, filter]);

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
        {t("settings.failedToLoad", { message: (settings.error as Error).message })}
      </Alert>
    );
  }

  const hasPending = (restartPending.data ?? []).length > 0;
  const visibleItems = itemsByTab[tab];

  return (
    <Box sx={{ p: 2, maxWidth: 1200 }}>
      {hasPending && (
        <Alert
          severity="warning"
          sx={{ mb: 2 }}
          action={
            <Button
              size="small"
              color="inherit"
              onClick={() => setRestartHelpOpen(true)}
            >
              {t("settings.howToRestart")}
            </Button>
          }
        >
          <strong>{t("settings.settingsRequireRestart", { count: String(restartPending.data!.length) })}</strong>{" "}
          {t("settings.recentlyChanged")}{" "}
          {restartPending
            .data!.slice(0, 4)
            .map((p) => p.setting_key)
            .join(", ")}
          {restartPending.data!.length > 4 ? t("settings.ellipsisMore") : ""}
        </Alert>
      )}

      <Stack direction="row" alignItems="center" spacing={2} sx={{ mb: 1 }}>
        <Typography variant="h6" sx={{ flexShrink: 0 }}>
          {t("settings.systemConfiguration")}
        </Typography>
        <HelpIconButton href="/help/system-admin/system-configuration.html" />
        <TextField
          size="small"
          placeholder={t("settings.searchKeys")}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          sx={{ flex: 1 }}
        />
      </Stack>

      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v as TabKey)}
        sx={{ borderBottom: 1, borderColor: "divider", mb: 2 }}
      >
        <Tab value="auth" label={`${t("settings.tabAuth")} (${itemsByTab.auth.length})`} />
        <Tab
          value="scheduling"
          label={`${t("settings.tabScheduling")} (${itemsByTab.scheduling.length})`}
        />
        <Tab value="timeouts" label={`${t("settings.tabTimeouts")} (${itemsByTab.timeouts.length})`} />
        <Tab value="ceilings" label={`${t("settings.tabCeilings")} (${itemsByTab.ceilings.length})`} />
        <Tab value="bootstrap" label={t("settings.tabBootstrap")} />
      </Tabs>

      {tab !== "bootstrap" && visibleItems.length === 0 && (
        <Typography color="text.secondary">{t("settings.noKeysMatch")}</Typography>
      )}

      {tab !== "bootstrap" && visibleItems.length > 0 && (
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" },
            gap: 2,
          }}
        >
          {visibleItems.map((item) => (
            <Box
              key={item.key}
              sx={{
                p: 1.5,
                border: 1,
                borderColor: "divider",
                borderRadius: 1,
              }}
            >
              <SettingFieldRenderer
                item={item}
                saving={savingKey === item.key && writeMutation.isPending}
                onSave={async (value) => {
                  setSavingKey(item.key);
                  try {
                    await writeMutation.mutateAsync({ key: item.key, value });
                    qc.invalidateQueries({
                      queryKey: ["system-settings", "restart-pending"],
                    });
                  } finally {
                    setSavingKey(null);
                  }
                }}
              />
            </Box>
          ))}
        </Box>
      )}

      {tab === "bootstrap" && (
        <Box>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {t("settings.bootstrapDescription")}
          </Typography>
          {bootstrap.isLoading && <CircularProgress size={18} />}
          {bootstrap.data && (
            <Box>
              {bootstrap.data.map((b: BootstrapItem) => (
                <Box
                  key={b.name}
                  sx={{
                    display: "grid",
                    gridTemplateColumns: "240px 1fr",
                    py: 0.75,
                    borderBottom: 1,
                    borderColor: "divider",
                    alignItems: "center",
                  }}
                >
                  <Typography sx={{ fontFamily: "monospace", fontSize: 13 }}>
                    {b.name}
                  </Typography>
                  <Box>
                    <Typography sx={{ fontFamily: "monospace", fontSize: 13 }}>
                      {b.value}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {b.description}
                    </Typography>
                  </Box>
                </Box>
              ))}
            </Box>
          )}
        </Box>
      )}

      <Dialog
        open={restartHelpOpen}
        onClose={() => setRestartHelpOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{t("settings.howToRestartTitle")}</DialogTitle>
        <DialogContent>
          <DialogContentText component="div">
            <Box sx={{ whiteSpace: "pre-wrap" }}>{t("settings.restartHowToContent")}</Box>
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button
            onClick={async () => {
              await systemSettingsApi.clearRestartPending();
              qc.invalidateQueries({
                queryKey: ["system-settings", "restart-pending"],
              });
              setRestartHelpOpen(false);
            }}
          >
            {t("settings.markAllApplied")}
          </Button>
          <Button onClick={() => setRestartHelpOpen(false)} variant="contained">
            {t("settings.close")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
