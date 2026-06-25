import { useEffect, useState } from "react";
import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Tab,
  Tabs,
  Typography,
} from "@mui/material";
import type { ModelTable } from "../../../api/types";
import DetailsTab from "./DetailsTab";
import ColumnsTab from "./ColumnsTab";
import AttributesTab from "./AttributesTab";
import ClassificationTab from "./ClassificationTab";
import { useT } from "../../../i18n";

export type TableEditTab = "table-details" | "business-description" | "attributes" | "classification";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
  sourceId: string;
  table: ModelTable;
  /**
   * Connection id behind the source. Used by the per-tab Sync Columns button
   * to call `connectionsApi.discoverColumns`. When null, sync is disabled
   * with a warning.
   */
  connectionId: string | null;
  initialTab?: TableEditTab;
}

export default function TableEditDialog({
  open,
  onClose,
  projectId,
  modelId,
  sourceId,
  table,
  connectionId,
  initialTab = "table-details",
}: Props) {
  const t = useT();
  const [tab, setTab] = useState<TableEditTab>(initialTab);

  useEffect(() => {
    if (open) setTab(initialTab);
  }, [open, initialTab]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pb: 0.5 }}>
        {t("tableEdit.dialogTitle")}
        <Typography variant="caption" color="text.secondary" display="block">
          {table.display_name || table.alias}
          {table.alias !== table.display_name && (
            <span style={{ marginLeft: 6, opacity: 0.7 }}>· {table.alias}</span>
          )}
          <span style={{ marginLeft: 6, opacity: 0.7 }}>· {table.physical_name}</span>
        </Typography>
      </DialogTitle>
      <Box sx={{ borderBottom: 1, borderColor: "divider", px: 3 }}>
        <Tabs value={tab} onChange={(_, v) => setTab(v as TableEditTab)}>
          <Tab value="table-details" label={t("tableEdit.tabTableDetails")} />
          <Tab value="classification" label={t("tableEdit.tabClassification")} />
          <Tab value="business-description" label={t("tableEdit.tabBusinessDescription")} />
          <Tab value="attributes" label={t("tableEdit.tabAttributesLabel")} />
        </Tabs>
      </Box>
      <DialogContent dividers>
        <Box sx={{ display: tab === "table-details" ? "block" : "none" }}>
          <DetailsTab
            projectId={projectId}
            modelId={modelId}
            sourceId={sourceId}
            table={table}
            connectionId={connectionId}
          />
        </Box>
        <Box sx={{ display: tab === "classification" ? "block" : "none" }}>
          <ClassificationTab
            projectId={projectId}
            modelId={modelId}
            sourceId={sourceId}
            table={table}
          />
        </Box>
        <Box sx={{ display: tab === "business-description" ? "block" : "none" }}>
          <ColumnsTab
            projectId={projectId}
            modelId={modelId}
            table={table}
            connectionId={connectionId}
          />
        </Box>
        <Box sx={{ display: tab === "attributes" ? "block" : "none" }}>
          <AttributesTab
            projectId={projectId}
            modelId={modelId}
            table={table}
            connectionId={connectionId}
          />
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("common.close")}</Button>
      </DialogActions>
    </Dialog>
  );
}
