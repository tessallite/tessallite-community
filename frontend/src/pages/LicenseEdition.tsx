import { Alert, Box, Paper, Typography } from "@mui/material";
import { safeLocalGet } from "../utils/safeLocalStorage";
import LicenseAndEdition from "../components/LicenseAndEdition";
import LicenseManagerCard from "../components/LicenseManagerCard";
import AdvisoryPanel from "../components/AdvisoryPanel";
import { useT } from "../i18n";

/**
 * License, edition, and advisories admin view (system-admin only). Cards: the
 * current edition + capped-resource usage, the License Manager (install/replace
 * the signed license live), and the security/update advisory feed.
 */
export default function LicenseEdition() {
  const t = useT();

  // Match the sibling /system/* pages: gate the page itself, not just the nav
  // entry, so a direct URL visit by a non-admin lands on a denied notice.
  const isSystemAdmin =
    typeof window !== "undefined" &&
    safeLocalGet("user_role", "") === "system_admin";

  if (!isSystemAdmin) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="warning">{t("systemAdmin.accessDenied")}</Alert>
      </Box>
    );
  }

  return (
    <Box
      sx={{
        flex: 1,
        minHeight: 0,
        overflow: "auto",
        bgcolor: "background.default",
        p: 3,
      }}
    >
      <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
        {t("license.pageTitle")}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t("license.pageSubtitle")}
      </Typography>

      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", md: "minmax(0, 1fr) minmax(0, 1fr)" },
          gap: 2,
          maxWidth: 1100,
        }}
      >
        <Paper variant="outlined" sx={{ p: 2 }}>
          <LicenseAndEdition />
        </Paper>
        <Paper variant="outlined" sx={{ p: 2 }}>
          <LicenseManagerCard />
        </Paper>
        <Paper variant="outlined" sx={{ p: 2 }}>
          <AdvisoryPanel />
        </Paper>
      </Box>
    </Box>
  );
}
