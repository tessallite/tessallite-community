import { Box, Typography } from "@mui/material";
import { useT } from "../../../i18n";

export default function ConnectBIStep() {
  const t = useT();

  return (
    <Box>
      <Typography variant="h6" gutterBottom>
        {t("wizardConnectBI.title")}
      </Typography>
      <Typography variant="body1" paragraph>
        {t("wizardConnectBI.body")}
      </Typography>

      <Typography variant="subtitle2" sx={{ mt: 2 }}>
        {t("wizardConnectBI.jdbcSection")}
      </Typography>
      <Typography
        variant="body2"
        sx={{ fontFamily: "monospace", bgcolor: "grey.100", p: 1, borderRadius: 1, mb: 2 }}
      >
        {t("wizardConnectBI.jdbcConnectionString")}
      </Typography>

      <Typography variant="subtitle2">
        {t("wizardConnectBI.xmlaSection")}
      </Typography>
      <Typography
        variant="body2"
        sx={{ fontFamily: "monospace", bgcolor: "grey.100", p: 1, borderRadius: 1, mb: 2 }}
      >
        {t("wizardConnectBI.xmlaUrl")}
      </Typography>

      <Typography variant="body2" color="text.secondary">
        {t("wizardConnectBI.footer")} {t("wizardConnectBI.seeHelp")}
      </Typography>
    </Box>
  );
}
