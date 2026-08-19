import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import { versionsApi, type GitCommitEntry } from "../../api/versionsApi";

type Props = {
  projectId: string;
  modelId: string;
};

const PAGE_SIZE = 50;

const NODE_RADIUS = { layout: 5, model: 8, restore: 8 } as const;

function commitColor(type: string): string {
  if (type === "layout") return "#90a4ae";
  if (type === "restore") return "#ff9800";
  return "#1976d2";
}

function CommitNode({
  entry,
  isLast,
  onViewDiff,
  t,
}: {
  entry: GitCommitEntry;
  isLast: boolean;
  onViewDiff: (sha: string) => void;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  const r = NODE_RADIUS[entry.type as keyof typeof NODE_RADIUS] ?? 6;
  const color = commitColor(entry.type);
  const hasDeployTag = entry.tags.some((tag) => tag.startsWith("deploy/"));

  let label: string;
  if (entry.type === "layout") {
    label = t("versions.timelineLayoutCommit");
  } else if (entry.type === "restore" && entry.version != null) {
    label = t("versions.timelineRestoreCommit", {
      n: String(entry.version),
    });
  } else if (entry.version != null) {
    label = `v${entry.version}`;
  } else {
    label = t("versions.timelineModelCommit");
  }

  return (
    <Box sx={{ display: "flex", minHeight: 48 }}>
      {/* Timeline column */}
      <Box
        sx={{
          width: 32,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          flexShrink: 0,
        }}
      >
        <svg width={20} height={r * 2 + 4} style={{ flexShrink: 0 }}>
          <circle
            cx={10}
            cy={r + 2}
            r={r}
            fill={color}
            stroke={color}
            strokeWidth={1.5}
          />
        </svg>
        {!isLast && (
          <Box
            sx={{
              width: 2,
              flex: 1,
              backgroundColor: "divider",
              minHeight: 16,
            }}
          />
        )}
      </Box>

      {/* Content column */}
      <Box sx={{ flex: 1, pb: 1, pl: 1 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            {label}
          </Typography>
          {hasDeployTag && (
            <Chip
              label={t("versions.timelineDeployTag")}
              color="success"
              size="small"
              sx={{ height: 20, fontSize: "0.7rem" }}
            />
          )}
        </Box>
        {entry.message && (
          <Typography variant="body2" color="text.secondary" noWrap>
            {entry.message}
          </Typography>
        )}
        <Box
          sx={{
            display: "flex",
            alignItems: "center",
            gap: 1,
            mt: 0.25,
          }}
        >
          <Typography variant="caption" color="text.disabled">
            {new Date(entry.timestamp).toLocaleString()}
          </Typography>
          <Typography variant="caption" color="text.disabled">
            {entry.author}
          </Typography>
          <Typography
            variant="caption"
            color="primary"
            sx={{ cursor: "pointer", ml: "auto" }}
            onClick={() => onViewDiff(entry.sha)}
          >
            {t("versions.timelineViewDiff")}
          </Typography>
        </Box>
      </Box>
    </Box>
  );
}

export default function GitTimeline({ projectId, modelId }: Props) {
  const t = useT();
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [diffSha, setDiffSha] = useState<{ sha1: string; sha2: string } | null>(null);

  const commitsQuery = useQuery({
    queryKey: ["git-log", projectId, modelId, limit],
    queryFn: () => versionsApi.gitLog(projectId, modelId, limit, 0),
  });

  const diffQuery = useQuery({
    queryKey: ["git-diff", projectId, modelId, diffSha?.sha1, diffSha?.sha2],
    queryFn: () =>
      versionsApi.gitDiff(projectId, modelId, diffSha!.sha1, diffSha!.sha2),
    enabled: !!diffSha,
  });

  function handleViewDiff(sha: string) {
    const commits = commitsQuery.data;
    if (!commits) return;
    const idx = commits.findIndex((c) => c.sha === sha);
    if (idx < 0 || idx >= commits.length - 1) return;
    setDiffSha({ sha1: commits[idx + 1].sha, sha2: sha });
  }

  if (commitsQuery.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  const commits = commitsQuery.data ?? [];

  if (commits.length === 0) {
    return (
      <Typography color="text.secondary" sx={{ py: 4, textAlign: "center" }}>
        {t("versions.timelineEmpty")}
      </Typography>
    );
  }

  return (
    <>
      <Box sx={{ py: 1 }}>
        {commits.map((entry, i) => (
          <CommitNode
            key={entry.sha}
            entry={entry}
            isLast={i === commits.length - 1}
            onViewDiff={handleViewDiff}
            t={t}
          />
        ))}
        {commits.length >= limit && (
          <Box sx={{ textAlign: "center", py: 1 }}>
            <Button
              size="small"
              onClick={() => setLimit((prev) => prev + PAGE_SIZE)}
            >
              {t("versions.timelineLoadMore")}
            </Button>
          </Box>
        )}
      </Box>

      {/* Diff modal */}
      <Dialog
        open={!!diffSha}
        onClose={() => setDiffSha(null)}
        maxWidth="md"
        fullWidth
      >
        <DialogTitle>{t("versions.timelineViewDiff")}</DialogTitle>
        <DialogContent>
          {diffQuery.isLoading && (
            <Box sx={{ p: 4, textAlign: "center" }}>
              <CircularProgress size={24} />
            </Box>
          )}
          {diffQuery.data != null && (
            <Box
              component="pre"
              sx={{
                fontFamily: "monospace",
                fontSize: "0.8rem",
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
                overflow: "auto",
                maxHeight: 500,
                p: 2,
                bgcolor: "grey.50",
                borderRadius: 1,
              }}
            >
              {diffQuery.data || "(no differences)"}
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDiffSha(null)}>{t("versions.close")}</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
