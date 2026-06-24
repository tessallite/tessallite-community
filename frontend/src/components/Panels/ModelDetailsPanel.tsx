import { useState } from "react";
import { Box, Tab, Tabs } from "@mui/material";
import { useT } from "../../i18n";
import ModelTab from "./ModelDetails/ModelTab";
import ModelDocsPanel from "./ModelDocsPanel";

/**
 * Model Details panel — two tabs:
 *   - Model: persona-filtered attribute table, export, and the source SELECT.
 *   - Documents: auto-generated model documentation (copy / download).
 */
export default function ModelDetailsPanel() {
  const t = useT();
  const [tab, setTab] = useState<"model" | "documents">("model");

  return (
    <Box>
      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v as "model" | "documents")}
        sx={{ mb: 2, borderBottom: 1, borderColor: "divider" }}
      >
        <Tab value="model" label={t("modelDetails.tabModel")} />
        <Tab value="documents" label={t("modelDetails.tabDocuments")} />
      </Tabs>

      {tab === "model" ? <ModelTab /> : <ModelDocsPanel />}
    </Box>
  );
}
