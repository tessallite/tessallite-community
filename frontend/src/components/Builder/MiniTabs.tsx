import { Tab, Tabs } from "@mui/material";
import { useBuilderStore, type MiniTab } from "../../store/builderStore";
import { useT } from "../../i18n";

// Keep this list in the same visible order used by the plain-digit shortcuts
// in useGlobalShortcuts: canvas, query, KPI scorecard, Model Health, analytics.
// "matrix" remains the internal store value for the Model Health tab.
const TAB_KEYS: { value: MiniTab; key: string }[] = [
  { value: "canvas", key: "builder.canvas" },
  { value: "query", key: "builder.query" },
  { value: "kpi-scorecard", key: "builder.kpiScorecard" },
  { value: "matrix", key: "builder.health" },
  { value: "analytics", key: "builder.analytics" },
];

export default function MiniTabs() {
  const miniTab = useBuilderStore((s) => s.miniTab);
  const setMiniTab = useBuilderStore((s) => s.setMiniTab);
  const t = useT();

  return (
    <Tabs
      id="model-builder-mini-tabs"
      aria-label={t("builder.aria.miniTabsLabel")}
      value={miniTab}
      onChange={(_, v: MiniTab) => setMiniTab(v)}
      sx={{ minHeight: 36, "& .MuiTab-root": { minHeight: 36, py: 0 } }}
    >
      {TAB_KEYS.map((tab) => (
        <Tab key={tab.value} value={tab.value} label={t(tab.key)} data-testid={`minitab-${tab.value}`} />
      ))}
    </Tabs>
  );
}
