import { Box, Typography } from "@mui/material";
import { useT } from "../../../i18n";

export default function WelcomeStep() {
  const t = useT();

  return (
    <Box>
      <Typography variant="h6" gutterBottom>
        {t("wizardWelcome.title")}
      </Typography>
      <Typography variant="body1" paragraph>
        {t("wizardWelcome.platformDescription")}
      </Typography>
      <Typography variant="subtitle2" gutterBottom>
        {t("wizardWelcome.keyConcepts")}
      </Typography>
      <Typography variant="body2" component="ul" sx={{ pl: 2 }}>
        <li>
          <strong>{t("wizardWelcome.projectTerm")}</strong>{" "}
          — {t("wizardWelcome.projectDesc")}
        </li>
        <li>
          <strong>{t("wizardWelcome.modelTerm")}</strong>{" "}
          — {t("wizardWelcome.modelDesc")}
        </li>
        <li>
          <strong>{t("wizardWelcome.connectionTerm")}</strong>{" "}
          — {t("wizardWelcome.connectionDesc")}
        </li>
        <li>
          <strong>{t("wizardWelcome.measureTerm")}</strong>{" "}
          — {t("wizardWelcome.measureDesc")}
        </li>
        <li>
          <strong>{t("wizardWelcome.dimensionTerm")}</strong>{" "}
          — {t("wizardWelcome.dimensionDesc")}
        </li>
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
        {t("wizardWelcome.wizardIntro")}
      </Typography>
    </Box>
  );
}
