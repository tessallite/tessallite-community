import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Box,
  Button,
  Paper,
  Typography,
} from "@mui/material";
import { useT } from "../i18n";
import { clearSession } from "../api/auth";

export default function SessionExpiredOverlay() {
  const t = useT();
  const [visible, setVisible] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    function handler() {
      setVisible(true);
    }
    window.addEventListener("tessallite:session-expired", handler);
    return () =>
      window.removeEventListener("tessallite:session-expired", handler);
  }, []);

  const handleSignIn = useCallback(() => {
    clearSession();
    setVisible(false);
    navigate("/login", { replace: true });
  }, [navigate]);

  if (!visible) return null;

  return (
    <Box
      sx={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        zIndex: (t) => t.zIndex.modal + 100,
        pointerEvents: "none",
        display: "flex",
        justifyContent: "center",
        pt: 2,
      }}
    >
      <Paper
        elevation={8}
        sx={{
          p: 3,
          maxWidth: 420,
          textAlign: "center",
          pointerEvents: "auto",
        }}
      >
        <Typography variant="h6" gutterBottom>
          {t("session.expired")}
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {t("session.expiredBody")}
        </Typography>
        <Button variant="contained" size="large" onClick={handleSignIn}>
          {t("session.signIn")}
        </Button>
      </Paper>
    </Box>
  );
}
