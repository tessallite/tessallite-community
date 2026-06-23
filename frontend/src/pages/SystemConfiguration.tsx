import { Alert, Box } from "@mui/material";
import { safeLocalGet } from "../utils/safeLocalStorage";
import SystemConfigurationTab from "../components/Settings/SystemConfigurationTab";
import { useT } from "../i18n";

export default function SystemConfiguration() {
  const t = useT();
  const isSystemAdmin =
    typeof window !== "undefined" &&
    safeLocalGet("user_role", "") === "system_admin";

  if (!isSystemAdmin) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="warning">
          {t("systemConfig.accessDenied")}
        </Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ flex: 1, minHeight: 0, overflow: "auto" }}>
      <SystemConfigurationTab />
    </Box>
  );
}
