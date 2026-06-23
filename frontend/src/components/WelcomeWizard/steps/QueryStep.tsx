import { Box, Typography } from "@mui/material";
import { useT } from "../../../i18n";

export default function QueryStep() {
  const t = useT();

  return (
    <Box>
      <Typography variant="h6" gutterBottom>
        {t("wizardQuery.title")}
      </Typography>
      <Typography variant="body1" paragraph>
        {t("wizardQuery.openQuery")}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        {t("wizardQuery.semantic")}
      </Typography>
    </Box>
  );
}
