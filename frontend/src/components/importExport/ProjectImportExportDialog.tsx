import { useEffect, useState } from "react";
import {
  Alert,
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
import { useProjects } from "../../api/hooks";
import ProjectImportPanel from "./panels/ProjectImportPanel";
import ProjectExportPanel from "./panels/ProjectExportPanel";

type ActiveTab = "import" | "export";

type Props = {
  open: boolean;
  onClose: () => void;
  onProjectImported?: () => void;
};

export default function ProjectImportExportDialog({
  open,
  onClose,
  onProjectImported,
}: Props) {
  const t = useT();
  const [activeTab, setActiveTab] = useState<ActiveTab>("import");
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [selectedProjectSlug, setSelectedProjectSlug] = useState("");
  const { data: projects } = useProjects();

  useEffect(() => {
    if (open) {
      setActiveTab("import");
      setSelectedProjectId("");
      setSelectedProjectSlug("");
    }
  }, [open]);

  function handleProjectSelect(projectId: string) {
    setSelectedProjectId(projectId);
    const proj = projects?.find((p) => p.id === projectId);
    setSelectedProjectSlug(proj?.slug ?? "");
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{t("projectImportExport.title")}</DialogTitle>
      <DialogContent>
        <Tabs
          value={activeTab}
          onChange={(_, v: ActiveTab) => setActiveTab(v)}
          sx={{ borderBottom: 1, borderColor: "divider", mb: 2 }}
        >
          <Tab label={t("projectImportExport.importTab")} value="import" />
          <Tab label={t("projectImportExport.exportTab")} value="export" />
        </Tabs>

        {activeTab === "import" && (
          <>
            <FormControl fullWidth size="small" sx={{ mb: 3 }}>
              <InputLabel>{t("projectImportExport.formatLabel")}</InputLabel>
              <Select
                value="project"
                label={t("projectImportExport.formatLabel")}
              >
                <MenuItem value="project">
                  {t("projectImportExport.formatProject")}
                </MenuItem>
              </Select>
            </FormControl>

            <ProjectImportPanel onImported={onProjectImported} />
          </>
        )}

        {activeTab === "export" && (
          <>
            <FormControl fullWidth size="small" sx={{ mb: 3 }}>
              <InputLabel>{t("projectImportExport.selectProject")}</InputLabel>
              <Select
                value={selectedProjectId}
                label={t("projectImportExport.selectProject")}
                onChange={(e) => handleProjectSelect(e.target.value)}
              >
                {(projects ?? []).map((p) => (
                  <MenuItem key={p.id} value={p.id}>
                    {p.display_name} ({p.slug})
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            {!selectedProjectId && (
              <Alert severity="info">
                {t("projectImportExport.selectProjectHint")}
              </Alert>
            )}

            {selectedProjectId && (
              <Box key={selectedProjectId}>
                <ProjectExportPanel
                  projectId={selectedProjectId}
                  projectSlug={selectedProjectSlug}
                  onDone={onClose}
                />
              </Box>
            )}
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("common.close")}</Button>
      </DialogActions>
    </Dialog>
  );
}
