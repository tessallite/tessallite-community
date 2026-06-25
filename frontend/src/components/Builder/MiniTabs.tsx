import { Tab, Tabs } from "@mui/material";
import { useBuilderStore, type MiniTab } from "../../store/builderStore";
import { useT } from "../../i18n";

// Two-way toggle between the Canvas (star-schema editor) and the
// Model Health tab. The previously-planned "Data" tab was removed
// in Phase G2 — the Diagnostics Query Log already surfaces real
// query results, so a dedicated data preview would duplicate
// existing UI without clear value. Add it back when there's a
// concrete use case that the Query Log doesn't cover.
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
