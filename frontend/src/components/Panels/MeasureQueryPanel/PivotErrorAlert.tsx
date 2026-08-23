/**
 * Error surface for the pivot panel (Bug-8182).
 *
 * Leads with a friendly, translated message. Any raw backend/transport text lives
 * behind a collapsed accordion (unmounted until opened) so recovery never puts
 * internal detail front-and-centre. Reuses the app's standard MUI Accordion.
 */
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import { useT } from "../../../i18n";
import type { PivotPanelError } from "./pivotErrors";

export default function PivotErrorAlert({ error }: { error: PivotPanelError }) {
  const t = useT();
  return (
    <Alert severity="error">
      {error.message}
      {error.detail ? (
        <Accordion
          disableGutters
          square
          variant="outlined"
          TransitionProps={{ unmountOnExit: true }}
          sx={{ mt: 0.5, bgcolor: "transparent", "&:before": { display: "none" } }}
        >
          <AccordionSummary
            expandIcon={<ExpandMoreIcon fontSize="small" />}
            sx={{ minHeight: 32, "& .MuiAccordionSummary-content": { my: 0.25 } }}
          >
            <Typography variant="caption" sx={{ fontWeight: 600 }}>
              {t("pivot.errorDetailsToggle")}
            </Typography>
          </AccordionSummary>
          <AccordionDetails sx={{ pt: 0, pb: 1 }}>
            <Typography
              variant="caption"
              component="pre"
              sx={{ whiteSpace: "pre-wrap", fontFamily: "monospace", m: 0 }}
            >
              {error.detail}
            </Typography>
          </AccordionDetails>
        </Accordion>
      ) : null}
    </Alert>
  );
}
