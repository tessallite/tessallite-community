import { useMemo } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Alert, Box, Button, CircularProgress, Typography } from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import { modelsApi } from "../../api/client";
import { useLineage, useDataTags } from "../../api/hooks";
import { useT } from "../../i18n";
import LineageGraph from "../LineageGraph";

export default function LineagePanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();

  const lineage = useLineage(projectId!, modelId!);
  const dataTags = useDataTags(projectId!, modelId!);

  const taggedColumnCounts = useMemo(() => {
    const map = new Map<string, number>();
    for (const tag of dataTags.data ?? []) {
      for (const col of tag.columns) {
        map.set(col.table_name, (map.get(col.table_name) ?? 0) + 1);
      }
    }
    return map;
  }, [dataTags.data]);

  const exportModel = useQuery({
    queryKey: ["export", projectId, modelId],
    queryFn: () => modelsApi.export(projectId!, modelId!),
    enabled: false,
  });

  function handleExport() {
    exportModel.refetch().then(({ data }) => {
      if (!data) return;
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `model-${modelId}-export.json`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  if (lineage.isLoading) return <CircularProgress size={20} />;

  if (lineage.isError) {
    return <Alert severity="error">{t("lineage.failedToLoad")}</Alert>;
  }

  if (!lineage.data || lineage.data.nodes.length === 0) {
    return (
      <Alert severity="info">
        {t("lineage.noDataMessage")}
      </Alert>
    );
  }

  return (
    <Box>
      <Box display="flex" mb={1} gap={1}>
        {[
          { color: "#e3f2fd", border: "#1976d2", labelKey: "lineage.nodeTypeSource" },
          { color: "#e8f5e9", border: "#388e3c", labelKey: "lineage.nodeTypeSemantic" },
          { color: "#f3e5f5", border: "#7c4dff", labelKey: "lineage.nodeTypeAggregate" },
          { color: "#fff3e0", border: "#ef6c00", labelKey: "lineage.nodeTypeTarget" },
        ].map(({ color, border, labelKey }) => (
          <Box
            key={labelKey}
            sx={{
              px: 1,
              py: 0.25,
              borderRadius: 1,
              bgcolor: color,
              border: `1px solid ${border}`,
              fontSize: 11,
            }}
          >
            {t(labelKey)}
          </Box>
        ))}
      </Box>
      <Box sx={{ height: 400 }}>
        <LineageGraph data={lineage.data} taggedColumnCounts={taggedColumnCounts} />
      </Box>
      <Box display="flex" alignItems="center" mt={1} gap={1}>
        <Typography variant="caption" color="text.secondary" flexGrow={1}>
          {t("lineage.nodesEdgesCount", {
            nodes: lineage.data.nodes.length.toString(),
            edges: lineage.data.edges.length.toString(),
          })}
        </Typography>
        <Button
          size="small"
          variant="outlined"
          startIcon={<DownloadIcon />}
          onClick={handleExport}
          disabled={exportModel.isFetching}
        >
          {t("lineage.exportJsonButton")}
        </Button>
      </Box>
    </Box>
  );
}
