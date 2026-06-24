import { Box, Typography } from "@mui/material";
import { useT } from "../../../i18n";

export default function SourceStep() {
  const t = useT();

  return (
    <Box>
      <Typography variant="h6" gutterBottom>
        {t("wizardSource.title")}
      </Typography>
      <Typography variant="body1" paragraph>
        {t("wizardSource.selectTables")}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        {t("wizardSource.moreSources")}
      </Typography>
    </Box>
  );
}
