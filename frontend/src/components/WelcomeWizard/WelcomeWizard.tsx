import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  Box,
  Button,
  Container,
  Link,
  Paper,
  Step,
  StepLabel,
  Stepper,
  Typography,
} from "@mui/material";
import { authApi } from "../../api/client";
import type { User } from "../../api/types";
import { useT } from "../../i18n";
import WelcomeStep from "./steps/WelcomeStep";
import SourceStep from "./steps/SourceStep";
import ModelStep from "./steps/ModelStep";
import QueryStep from "./steps/QueryStep";
import ConnectBIStep from "./steps/ConnectBIStep";

export default function WelcomeWizard() {
  const t = useT();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [activeStep, setActiveStep] = useState(0);
  const [completing, setCompleting] = useState(false);

  const STEPS = [
    { label: t("wizard.stepLabel.welcome"), component: WelcomeStep },
    { label: t("wizard.stepLabel.addSources"), component: SourceStep },
    { label: t("wizard.stepLabel.buildModel"), component: ModelStep },
    { label: t("wizard.stepLabel.runQuery"), component: QueryStep },
    { label: t("wizard.stepLabel.connectBI"), component: ConnectBIStep },
  ];

  const isLast = activeStep === STEPS.length - 1;
  const StepComponent = STEPS[activeStep].component;

  async function markOnboardingDone() {
    try {
      await authApi.completeOnboarding();
    } catch {
      // best-effort
    }
    qc.setQueryData<User>(["me"], (old) =>
      old ? { ...old, has_completed_onboarding: true } : old,
    );
    await qc.invalidateQueries({ queryKey: ["me"] });
    navigate("/", { replace: true });
  }

  async function handleFinish() {
    setCompleting(true);
    await markOnboardingDone();
  }

  async function handleSkipAll() {
    setCompleting(true);
    await markOnboardingDone();
  }

  return (
    <Container maxWidth="md" sx={{ py: 4 }}>
      <Paper sx={{ p: 4 }}>
        <Typography variant="h5" gutterBottom>
          {t("wizard.gettingStarted")}
        </Typography>

        <Stepper activeStep={activeStep} sx={{ mb: 4 }}>
          {STEPS.map((s) => (
            <Step key={s.label}>
              <StepLabel>{s.label}</StepLabel>
            </Step>
          ))}
        </Stepper>

        <Box sx={{ minHeight: 240, mb: 3 }}>
          <StepComponent />
        </Box>

        <Box display="flex" justifyContent="space-between" alignItems="center">
          <Link
            component="button"
            variant="body2"
            color="text.secondary"
            onClick={handleSkipAll}
            disabled={completing}
            sx={{ textDecoration: "none" }}
          >
            {t("wizard.skipAll")}
          </Link>

          <Box display="flex" gap={1}>
            <Button
              disabled={activeStep === 0}
              onClick={() => setActiveStep((s) => s - 1)}
            >
              {t("wizard.back")}
            </Button>
            {activeStep < STEPS.length - 1 && (
              <Button
                variant="text"
                onClick={() => setActiveStep((s) => s + 1)}
              >
                {t("wizard.skip")}
              </Button>
            )}
            {isLast ? (
              <Button
                variant="contained"
                onClick={handleFinish}
                disabled={completing}
              >
                {t("wizard.finish")}
              </Button>
            ) : (
              <Button
                variant="contained"
                onClick={() => setActiveStep((s) => s + 1)}
              >
                {t("wizard.next")}
              </Button>
            )}
          </Box>
        </Box>
      </Paper>
    </Container>
  );
}
