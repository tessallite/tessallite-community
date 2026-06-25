import { useEffect, useState } from "react";
import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Tab,
  Tabs,
} from "@mui/material";
import { useT } from "../../i18n";
import ModelImportPanel from "./panels/ModelImportPanel";
import YamlImportPanel from "./panels/YamlImportPanel";
import DbtImportPanel from "./panels/DbtImportPanel";
import CubeImportPanel from "./panels/CubeImportPanel";
import AtScaleImportPanel from "./panels/AtScaleImportPanel";
import CatalogImportPanel from "./panels/CatalogImportPanel";
import ModelExportPanel from "./panels/ModelExportPanel";
import YamlExportPanel from "./panels/YamlExportPanel";
import LookMLExportPanel from "./panels/LookMLExportPanel";

type ImportFormat = "model" | "yaml" | "dbt" | "cube" | "atscale" | "catalog";
type ExportFormat = "model" | "yaml" | "lookml";
type ActiveTab = "import" | "export";

type Props = {
  open: boolean;
  onClose: () => void;
  projectId: string;
  projectSlug: string;
  onModelImported?: (modelId: string) => void;
};

const IMPORT_FORMATS: { value: ImportFormat; i18nKey: string }[] = [
  { value: "model", i18nKey: "modelImportExport.formatModel" },
  { value: "yaml", i18nKey: "modelImportExport.formatYaml" },
  { value: "dbt", i18nKey: "modelImportExport.formatDbt" },
  { value: "cube", i18nKey: "modelImportExport.formatCube" },
  { value: "atscale", i18nKey: "modelImportExport.formatAtscale" },
  { value: "catalog", i18nKey: "modelImportExport.formatCatalog" },
];

const EXPORT_FORMATS: { value: ExportFormat; i18nKey: string }[] = [
  { value: "model", i18nKey: "modelImportExport.formatModel" },
  { value: "yaml", i18nKey: "modelImportExport.formatYaml" },
  { value: "lookml", i18nKey: "modelImportExport.formatLookml" },
];

export default function ModelImportExportDialog({
  open,
  onClose,
  projectId,
  projectSlug,
  onModelImported,
}: Props) {
  const t = useT();
  const [activeTab, setActiveTab] = useState<ActiveTab>("import");
  const [importFormat, setImportFormat] = useState<ImportFormat>("model");
  const [exportFormat, setExportFormat] = useState<ExportFormat>("model");

  useEffect(() => {
    if (open) {
      setActiveTab("import");
      setImportFormat("model");
      setExportFormat("model");
    }
  }, [open]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{t("modelImportExport.title")}</DialogTitle>
      <DialogContent>
        <Tabs
          value={activeTab}
          onChange={(_, v: ActiveTab) => setActiveTab(v)}
          sx={{ borderBottom: 1, borderColor: "divider", mb: 2 }}
        >
          <Tab label={t("modelImportExport.importTab")} value="import" />
          <Tab label={t("modelImportExport.exportTab")} value="export" />
        </Tabs>

        {activeTab === "import" && (
          <>
            <FormControl fullWidth size="small" sx={{ mb: 3 }}>
              <InputLabel>{t("modelImportExport.formatLabel")}</InputLabel>
              <Select
                value={importFormat}
                label={t("modelImportExport.formatLabel")}
                onChange={(e) => setImportFormat(e.target.value as ImportFormat)}
              >
                {IMPORT_FORMATS.map((f) => (
                  <MenuItem key={f.value} value={f.value}>
                    {t(f.i18nKey)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            <Box key={importFormat}>
              {importFormat === "model" && (
                <ModelImportPanel
                  projectId={projectId}
                  onImported={onModelImported}
                />
              )}
              {importFormat === "yaml" && (
                <YamlImportPanel projectId={projectId} />
              )}
              {importFormat === "dbt" && (
                <DbtImportPanel projectId={projectId} />
              )}
              {importFormat === "cube" && (
                <CubeImportPanel projectId={projectId} />
              )}
              {importFormat === "atscale" && (
                <AtScaleImportPanel projectId={projectId} />
              )}
              {importFormat === "catalog" && (
                <CatalogImportPanel projectId={projectId} />
              )}
            </Box>
          </>
        )}

        {activeTab === "export" && (
          <>
            <FormControl fullWidth size="small" sx={{ mb: 3 }}>
              <InputLabel>{t("modelImportExport.formatLabel")}</InputLabel>
              <Select
                value={exportFormat}
                label={t("modelImportExport.formatLabel")}
                onChange={(e) => setExportFormat(e.target.value as ExportFormat)}
              >
                {EXPORT_FORMATS.map((f) => (
                  <MenuItem key={f.value} value={f.value}>
                    {t(f.i18nKey)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            <Box key={exportFormat}>
              {exportFormat === "model" && (
                <ModelExportPanel projectId={projectId} onDone={onClose} />
              )}
              {exportFormat === "yaml" && (
                <YamlExportPanel
                  projectId={projectId}
                  projectSlug={projectSlug}
                  onDone={onClose}
                />
              )}
              {exportFormat === "lookml" && (
                <LookMLExportPanel projectId={projectId} onDone={onClose} />
              )}
            </Box>
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("common.close")}</Button>
      </DialogActions>
    </Dialog>
  );
}
