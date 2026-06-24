import { Box, Typography } from "@mui/material";
import { useT } from "../../../i18n";

export default function ModelStep() {
  const t = useT();

  return (
    <Box>
      <Typography variant="h6" gutterBottom>
        {t("wizardModel.title")}
      </Typography>
      <Typography variant="body1" paragraph>
        {t("wizardModel.fullBody")}
      </Typography>
      <Typography variant="body2" component="ul" sx={{ pl: 2 }}>
        <li>
          <strong>{t("wizardModel.measuresTerm")}</strong>{" "}
          {t("wizardModel.measuresDesc")}
        </li>
        <li>
          <strong>{t("wizardModel.dimensionsTerm")}</strong>{" "}
          {t("wizardModel.dimensionsDesc")}
        </li>
        <li>
          <strong>{t("wizardModel.joinsTerm")}</strong>{" "}
          {t("wizardModel.joinsDesc")}
        </li>
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
        {t("wizardModel.footer")}
      </Typography>
    </Box>
  );
}
