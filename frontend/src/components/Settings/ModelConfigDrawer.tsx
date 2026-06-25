import { useState } from "react";
import {
  Box,
  Drawer,
  IconButton,
  Tooltip,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import KeyboardDoubleArrowLeftIcon from "@mui/icons-material/KeyboardDoubleArrowLeft";
import KeyboardDoubleArrowRightIcon from "@mui/icons-material/KeyboardDoubleArrowRight";
import SettingsPanel from "../Panels/SettingsPanel";
import HelpIconButton from "../HelpIconButton";
import { useT } from "../../i18n";

export default function ModelConfigDrawer({
  projectId,
  modelId,
  modelName,
  open,
  onClose,
}: {
  projectId: string;
  modelId: string;
  modelName: string;
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      sx={{ zIndex: (t) => t.zIndex.drawer + 2 }}
    >
      <Box sx={{ width: expanded ? "100vw" : 820, maxWidth: "100vw", display: "flex", flexDirection: "column", height: "100%" }}>
        <Box
          sx={{
            px: 2,
            py: 1.5,
            borderBottom: 1,
            borderColor: "divider",
            display: "flex",
            alignItems: "center",
          }}
        >
          <Box sx={{ flex: 1 }}>
            <Typography variant="overline" color="text.secondary">
              {t("modelConfig.modelConfiguration")}
            </Typography>
            <Typography variant="h6" sx={{ fontWeight: 700 }}>
              {modelName}
            </Typography>
          </Box>
          <HelpIconButton href="/help/admin/model-configuration.html" sx={{ mr: 0.5 }} />
          <Tooltip title={expanded ? t("drawer.retractPanel") : t("drawer.expandPanel")}>
            <IconButton size="small" onClick={() => setExpanded((v) => !v)} sx={{ mr: 0.5 }}>
              {expanded ? (
                <KeyboardDoubleArrowRightIcon />
              ) : (
                <KeyboardDoubleArrowLeftIcon />
              )}
            </IconButton>
          </Tooltip>
          <IconButton onClick={onClose} size="small">
            <CloseIcon />
          </IconButton>
        </Box>

        <Box sx={{ flex: 1, overflow: "auto", p: 2.5 }}>
          <SettingsPanel projectId={projectId} modelId={modelId} />
        </Box>
      </Box>
    </Drawer>
  );
}
