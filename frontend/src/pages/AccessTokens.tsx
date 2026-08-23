import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  InputAdornment,
  MenuItem,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AddIcon from "@mui/icons-material/Add";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import { useNavigate } from "react-router-dom";
import { patApi } from "../api/client";
import type {
  PersonalAccessToken,
  PersonalAccessTokenCreateResponse,
} from "../api/types";
import { useConfirm } from "../components/Confirm";
import { useT } from "../i18n";

// Non-expiring is the "0"/omitted option; the rest cap at the backend limit (365).
const EXPIRY_OPTIONS = [0, 30, 90, 180, 365];

function isExpired(pat: PersonalAccessToken): boolean {
  return (
    pat.expires_at !== null &&
    pat.expires_at !== undefined &&
    new Date(pat.expires_at).getTime() <= Date.now()
  );
}

function statusChip(pat: PersonalAccessToken, t: (k: string) => string) {
  if (pat.revoked_at) {
    return <Chip label={t("accessTokens.statusRevoked")} size="small" color="error" />;
  }
  if (isExpired(pat)) {
    return <Chip label={t("accessTokens.statusExpired")} size="small" color="warning" />;
  }
  return <Chip label={t("accessTokens.statusActive")} size="small" color="success" />;
}

export default function AccessTokens() {
  const t = useT();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const [createOpen, setCreateOpen] = useState(false);
  const [created, setCreated] = useState<PersonalAccessTokenCreateResponse | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const { data: tokens = [], isLoading } = useQuery({
    queryKey: ["pat-tokens"],
    queryFn: patApi.list,
  });

  const revokeMut = useMutation({
    mutationFn: (id: string) => patApi.revoke(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pat-tokens"] }),
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("accessTokens.revokeFailed")),
  });

  async function handleRevoke(pat: PersonalAccessToken) {
    const ok = await confirm({
      title: t("accessTokens.revokeConfirmTitle"),
      message: t("accessTokens.revokeConfirmBody", { label: pat.label || pat.token_prefix }),
      confirmLabel: t("accessTokens.revokeButton"),
      destructive: true,
    });
    if (ok) revokeMut.mutate(pat.id);
  }

  return (
    <Box sx={{ p: 3, maxWidth: 900, mx: "auto" }}>
      <Box display="flex" alignItems="center" gap={1} mb={1}>
        <IconButton size="small" onClick={() => navigate("/")}>
          <ArrowBackIcon />
        </IconButton>
        <Typography variant="h5" fontWeight={700}>
          {t("accessTokens.title")}
        </Typography>
      </Box>
      <Typography variant="body2" color="text.secondary" mb={2}>
        {t("accessTokens.description")}
      </Typography>

      <Alert severity="info" sx={{ mb: 2 }}>
        {t("accessTokens.ssoInfo")}
      </Alert>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Box display="flex" justifyContent="flex-end" mb={2}>
        <Button
          startIcon={<AddIcon />}
          variant="contained"
          size="small"
          onClick={() => {
            setError(null);
            setCreateOpen(true);
          }}
        >
          {t("accessTokens.generateButton")}
        </Button>
      </Box>

      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 700 }}>{t("accessTokens.colLabel")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("accessTokens.colToken")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("accessTokens.colStatus")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("accessTokens.colCreated")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("accessTokens.colExpires")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("accessTokens.colLastUsed")}</TableCell>
              <TableCell width={60} />
            </TableRow>
          </TableHead>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={7} align="center">
                  <CircularProgress size={18} />
                </TableCell>
              </TableRow>
            )}
            {!isLoading && tokens.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} align="center">
                  {t("accessTokens.noTokens")}
                </TableCell>
              </TableRow>
            )}
            {tokens.map((pat: PersonalAccessToken) => (
              <TableRow key={pat.id}>
                <TableCell>{pat.label || <em>{t("accessTokens.noLabel")}</em>}</TableCell>
                <TableCell>
                  <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                    {pat.token_prefix}…
                  </Typography>
                </TableCell>
                <TableCell>{statusChip(pat, t)}</TableCell>
                <TableCell>{new Date(pat.created_at).toLocaleDateString()}</TableCell>
                <TableCell>
                  {pat.expires_at
                    ? new Date(pat.expires_at).toLocaleDateString()
                    : t("accessTokens.never")}
                </TableCell>
                <TableCell>
                  {pat.last_used_at
                    ? new Date(pat.last_used_at).toLocaleString()
                    : t("accessTokens.neverUsed")}
                </TableCell>
                <TableCell>
                  {!pat.revoked_at && (
                    <Tooltip title={t("accessTokens.revokeTooltip")}>
                      <IconButton
                        size="small"
                        onClick={() => handleRevoke(pat)}
                        disabled={revokeMut.isPending}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <CreateTokenDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(resp) => {
          setCreateOpen(false);
          setCreated(resp);
          qc.invalidateQueries({ queryKey: ["pat-tokens"] });
        }}
      />

      {created !== null && (
        <TokenRevealDialog
          created={created}
          onClose={() => setCreated(null)}
        />
      )}
    </Box>
  );
}

function CreateTokenDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (resp: PersonalAccessTokenCreateResponse) => void;
}) {
  const t = useT();
  const [label, setLabel] = useState("");
  const [expiresInDays, setExpiresInDays] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const createMut = useMutation({
    mutationFn: () =>
      patApi.create({
        label: label.trim(),
        expires_in_days: expiresInDays > 0 ? expiresInDays : null,
      }),
    onSuccess: (resp) => {
      setLabel("");
      setExpiresInDays(0);
      setError(null);
      onCreated(resp);
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("accessTokens.createFailed")),
  });

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t("accessTokens.generateDialogTitle")}</DialogTitle>
      <DialogContent>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        <TextField
          label={t("accessTokens.labelField")}
          fullWidth
          margin="normal"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={t("accessTokens.labelPlaceholder")}
          autoFocus
          inputProps={{ maxLength: 255 }}
        />
        <TextField
          select
          label={t("accessTokens.expiryField")}
          fullWidth
          margin="normal"
          value={expiresInDays}
          onChange={(e) => setExpiresInDays(Number(e.target.value))}
        >
          {EXPIRY_OPTIONS.map((days) => (
            <MenuItem key={days} value={days}>
              {days === 0
                ? t("accessTokens.expiryNever")
                : t("accessTokens.expiryDays", { days })}
            </MenuItem>
          ))}
        </TextField>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button
          variant="contained"
          disabled={createMut.isPending}
          onClick={() => createMut.mutate()}
        >
          {createMut.isPending ? <CircularProgress size={16} /> : t("accessTokens.generateButton")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function TokenRevealDialog({
  created,
  onClose,
}: {
  created: PersonalAccessTokenCreateResponse;
  onClose: () => void;
}) {
  const t = useT();
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(created.token).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t("accessTokens.revealTitle")}</DialogTitle>
      <DialogContent>
        <Alert severity="warning" sx={{ mb: 2 }}>
          {t("accessTokens.revealWarning")}
        </Alert>
        <TextField
          fullWidth
          value={created.token}
          InputProps={{
            readOnly: true,
            sx: { fontFamily: "monospace", fontSize: 13 },
            endAdornment: (
              <InputAdornment position="end">
                <Tooltip title={copied ? t("endpoints.copiedToClipboard") : t("common.copy")}>
                  <IconButton size="small" onClick={handleCopy} edge="end">
                    <ContentCopyIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </InputAdornment>
            ),
          }}
          size="small"
        />
        <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
          {t("accessTokens.revealUsage")}
        </Typography>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="contained">
          {t("accessTokens.doneButton")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
