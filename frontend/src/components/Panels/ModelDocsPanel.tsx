import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  CircularProgress,
  IconButton,
  Tooltip,
  Typography,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DownloadIcon from "@mui/icons-material/Download";
import { modelDocsApi } from "../../api/client";
import { looksLikeMarkdown, markdownToHtml } from "../../lib/markdownToHtml";
import { sanitizeHtml } from "../../utils/sanitize";
import { useT } from "../../i18n";

/**
 * Documents tab of the Model Details panel. Documentation is lightweight to
 * produce, so it auto-generates on mount via a query — there is no manual
 * generate button. Copy and download remain available.
 */
export default function ModelDocsPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const [copied, setCopied] = useState(false);

  const docs = useQuery({
    queryKey: ["modelDocs", projectId, modelId],
    queryFn: () => modelDocsApi.generate(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });
  const markdown = docs.data?.markdown ?? null;

  function handleCopy() {
    if (!markdown) return;
    navigator.clipboard.writeText(markdown).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  function handleDownload() {
    if (!markdown) return;
    const blob = new Blob([markdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "model-documentation.md";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t("modelDocs.description")}
      </Typography>

      {docs.isError && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {(docs.error as { response?: { data?: { detail?: string } } })
            ?.response?.data?.detail ?? t("modelDocs.generateFailed")}
        </Alert>
      )}

      {docs.isLoading && (
        <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      )}

      {markdown && (
        <>
          <Box
            sx={{
              display: "flex",
              gap: 0.5,
              mb: 1,
              justifyContent: "flex-end",
            }}
          >
            <Tooltip title={copied ? t("modelDocs.copied") : t("modelDocs.copyMarkdown")}>
              <IconButton size="small" onClick={handleCopy}>
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title={t("modelDocs.downloadMarkdown")}>
              <IconButton size="small" onClick={handleDownload}>
                <DownloadIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Box>
          <Box
            sx={{
              border: 1,
              borderColor: "divider",
              borderRadius: 1,
              p: 2,
              maxHeight: "60vh",
              overflowY: "auto",
              bgcolor: "background.paper",
              typography: "body2",
              "& h1": { fontSize: 20, mt: 2, mb: 1 },
              "& h2": { fontSize: 16, mt: 2, mb: 1 },
              "& h3": { fontSize: 14, mt: 1.5, mb: 0.5 },
              "& table": {
                borderCollapse: "collapse",
                width: "100%",
                my: 1,
              },
              "& th, & td": {
                border: "1px solid",
                borderColor: "divider",
                px: 1,
                py: 0.5,
                fontSize: 12,
              },
              "& th": { fontWeight: 600, bgcolor: "action.hover" },
              "& code": {
                fontFamily: "monospace",
                fontSize: 12,
                bgcolor: "action.hover",
                px: 0.5,
                borderRadius: 0.5,
              },
            }}
          >
            {looksLikeMarkdown(markdown) ? (
              <div
                dangerouslySetInnerHTML={{
                  __html: sanitizeHtml(markdownToHtml(markdown)),
                }}
              />
            ) : (
              <Typography
                variant="body2"
                sx={{ whiteSpace: "pre-wrap", fontFamily: "monospace" }}
              >
                {markdown}
              </Typography>
            )}
          </Box>
        </>
      )}
    </Box>
  );
}
