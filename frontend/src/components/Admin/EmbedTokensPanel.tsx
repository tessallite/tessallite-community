import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import { embedTokensApi } from "../../api/client";

export default function EmbedTokensPanel() {
  const t = useT();
  const qc = useQueryClient();

  const listQ = useQuery({
    queryKey: ["embed-tokens"],
    queryFn: embedTokensApi.list,
  });

  const revokeMut = useMutation({
    mutationFn: (jti: string) => embedTokensApi.revoke(jti),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["embed-tokens"] });
    },
  });

  if (listQ.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  const rows = listQ.data ?? [];

  return (
    <Box sx={{ py: 2 }}>
      <Typography variant="h6" sx={{ mb: 1 }}>
        {t("embedTokens.title")}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {t("embedTokens.help")}
      </Typography>

      {listQ.isError && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {t("embedTokens.loadFailed")}
        </Alert>
      )}
      {revokeMut.isError && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {t("embedTokens.loadFailed")}
        </Alert>
      )}

      {rows.length === 0 && !listQ.isError ? (
        <Typography variant="body2" color="text.secondary">
          {t("embedTokens.empty")}
        </Typography>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t("embedTokens.user")}</TableCell>
              <TableCell>{t("embedTokens.expires")}</TableCell>
              <TableCell>{t("embedTokens.revoked")}</TableCell>
              <TableCell>{t("embedTokens.actions")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.jti}>
                <TableCell>{row.user_identity}</TableCell>
                <TableCell>{row.expires_at ?? "—"}</TableCell>
                <TableCell>{row.revoked_at ?? "—"}</TableCell>
                <TableCell>
                  <Stack direction="row">
                    <Button
                      size="small"
                      disabled={Boolean(row.revoked_at) || revokeMut.isPending}
                      onClick={() => revokeMut.mutate(row.jti)}
                    >
                      {t("embedTokens.revoke")}
                    </Button>
                  </Stack>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Box>
  );
}
