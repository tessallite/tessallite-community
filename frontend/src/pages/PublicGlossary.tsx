/**
 * PublicGlossary — Phase 4 of the semantic-layer plan.
 *
 * A read-only HTML page served by the same frontend nginx, reachable
 * via a tokenized URL (no login). Modellers share the link with their
 * non-technical co-workers; recipients can browse the glossary, search
 * client-side, and download CSV / XLSX without ever logging into the
 * Tessallite UI.
 */
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Container,
  Divider,
  Paper,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import SearchIcon from "@mui/icons-material/Search";
import axios from "axios";

// Bug-7957: the public payload contains ONLY what this page renders.
// No internal object IDs (model.id, attachment target_id) are included.
interface GlossaryEntry {
  term: string;
  definition: string;
  context_notes: string | null;
  synonyms: string[];
  version: number;
  updated_at: string;
}
interface GlossaryPayload {
  model: {
    slug: string;
    display_name: string | null;
    description: string | null;
  };
  entries: GlossaryEntry[];
}

import { modelServiceBaseUrl } from "../api/apiBase";
import { useT } from "../i18n";

const API_BASE = modelServiceBaseUrl();

export default function PublicGlossary() {
  const t = useT();
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<GlossaryPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    axios
      .get<GlossaryPayload>(`${API_BASE}/api/v1/glossary/public/${encodeURIComponent(token)}`)
      .then((r) => setData(r.data))
      .catch((err) => {
        // Bug-7964: map HTTP failure modes to i18n keys instead of
        // echoing backend error detail (English server wording on a
        // public page). 404 = invalid/expired/revoked link; anything
        // else = generic server error.
        const status = err?.response?.status;
        if (status === 404) {
          setError(t("publicGlossary.invalidLink"));
        } else {
          setError(t("publicGlossary.loadError"));
        }
      })
      .finally(() => setLoading(false));
  }, [token, t]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    if (!q) return data.entries;
    return data.entries.filter((e) => {
      if (e.term.toLowerCase().includes(q)) return true;
      if (e.definition.toLowerCase().includes(q)) return true;
      if ((e.context_notes ?? "").toLowerCase().includes(q)) return true;
      if (e.synonyms.some((s) => s.toLowerCase().includes(q))) return true;
      return false;
    });
  }, [data, search]);

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" minHeight="60vh">
        <CircularProgress />
      </Box>
    );
  }

  if (error) {
    return (
      <Container maxWidth="md" sx={{ py: 8 }}>
        <Alert severity="error">{error}</Alert>
      </Container>
    );
  }

  if (!data) {
    return null;
  }

  function downloadUrl(format: "csv" | "xlsx" | "pdf"): string {
    return `${API_BASE}/api/v1/glossary/public/${encodeURIComponent(token!)}/download.${format}`;
  }

  return (
    <Box sx={{ bgcolor: "grey.50", minHeight: "100vh", py: 4 }}>
      <Container maxWidth="md">
        <Paper sx={{ p: 3, mb: 2 }} elevation={1}>
          <Typography variant="overline" color="text.secondary">
            {t("publicGlossary.brand")}
          </Typography>
          <Typography variant="h4" fontWeight={700} gutterBottom>
            {data.model.display_name || data.model.slug}
          </Typography>
          {data.model.description && (
            <Typography variant="body2" color="text.secondary" gutterBottom>
              {data.model.description}
            </Typography>
          )}
          <Box display="flex" gap={1} mt={2}>
            <Button
              variant="outlined"
              size="small"
              startIcon={<DownloadIcon />}
              href={downloadUrl("csv")}
            >
              {t("publicGlossary.downloadCsv")}
            </Button>
            <Button
              variant="outlined"
              size="small"
              startIcon={<DownloadIcon />}
              href={downloadUrl("xlsx")}
            >
              {t("publicGlossary.downloadExcel")}
            </Button>
            <Button
              variant="outlined"
              size="small"
              startIcon={<DownloadIcon />}
              href={downloadUrl("pdf")}
            >
              {t("publicGlossary.downloadPdf")}
            </Button>
          </Box>
        </Paper>

        <Paper sx={{ p: 2, mb: 2 }} elevation={1}>
          <TextField
            fullWidth
            size="small"
            placeholder={t("publicGlossary.searchPlaceholder")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            InputProps={{ startAdornment: <SearchIcon sx={{ mr: 1, color: "text.secondary" }} /> }}
          />
          <Typography variant="caption" color="text.secondary" mt={0.5} display="block">
            {t("publicGlossary.entryCount", { visible: String(filtered.length), total: String(data.entries.length) })}
          </Typography>
        </Paper>

        <Stack spacing={1.5}>
          {filtered.map((entry) => (
            <Paper key={`${entry.term}-${entry.version}`} sx={{ p: 2 }} elevation={1}>
              <Typography variant="h6" fontWeight={700}>
                {entry.term}
              </Typography>
              <Typography variant="body2" color="text.primary" sx={{ mt: 0.5 }}>
                {entry.definition}
              </Typography>
              {entry.context_notes && (
                <>
                  <Divider sx={{ my: 1 }} />
                  <Typography variant="caption" color="text.secondary" fontStyle="italic">
                    {entry.context_notes}
                  </Typography>
                </>
              )}
              {entry.synonyms.length > 0 && (
                <Box mt={1} display="flex" gap={0.5} flexWrap="wrap">
                  {entry.synonyms.map((s) => (
                    <Chip key={s} label={s} size="small" variant="outlined" />
                  ))}
                </Box>
              )}
              <Typography variant="caption" color="text.disabled" display="block" mt={1}>
                {t("publicGlossary.versionUpdated", { version: String(entry.version), date: new Date(entry.updated_at).toLocaleDateString() })}
              </Typography>
            </Paper>
          ))}
          {filtered.length === 0 && (
            <Typography variant="body2" color="text.secondary" textAlign="center" sx={{ py: 4 }}>
              {t("publicGlossary.noResults")}
            </Typography>
          )}
        </Stack>
      </Container>
    </Box>
  );
}
