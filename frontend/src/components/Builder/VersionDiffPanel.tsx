import { useState } from "react";
import { useT } from "../../i18n";
import {
  Box,
  Chip,
  Divider,
  MenuItem,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import AddCircleOutlineIcon from "@mui/icons-material/AddCircleOutline";
import RemoveCircleOutlineIcon from "@mui/icons-material/RemoveCircleOutline";
import EditIcon from "@mui/icons-material/Edit";
import { useVersionDiff } from "../../api/versionsApi";
import type { VersionItem } from "../../api/versionsApi";

// Category labels are resolved at render time via useT() inside the component.

interface Props {
  projectId: string;
  modelId: string;
  versions: VersionItem[];
}

export default function VersionDiffPanel({ projectId, modelId, versions }: Props) {
  const t = useT();
  const CATEGORY_LABELS: Record<string, string> = {
    tables: t("versionDiff.categoryTables"),
    dimensions: t("versionDiff.categoryDimensions"),
    measures: t("versionDiff.categoryMeasures"),
    joins: t("versionDiff.categoryJoins"),
    hierarchies: t("versionDiff.categoryHierarchies"),
    aggregates: t("versionDiff.categoryAggregates"),
    pockets: t("versionDiff.categoryPockets"),
    personas: t("versionDiff.categoryPersonas"),
  };
  const [vaId, setVaId] = useState<string>(versions[1]?.id ?? "");
  const [vbId, setVbId] = useState<string>(versions[0]?.id ?? "");
  const diff = useVersionDiff(projectId, modelId, vaId || undefined, vbId || undefined);

  const totalChanges = diff.data
    ? Object.values(diff.data.diff).reduce(
        (n, cat) => n + cat.added.length + cat.removed.length + cat.changed.length,
        0,
      )
    : null;

  return (
    <Box>
      <Typography variant="subtitle2" fontWeight={700} mb={1}>
        {t("versionDiff.title")}
      </Typography>
      <Box display="flex" gap={1} alignItems="center" mb={2} flexWrap="wrap">
        <Select
          size="small"
          value={vaId}
          onChange={(e) => setVaId(e.target.value)}
          displayEmpty
          sx={{ minWidth: 120 }}
        >
          <MenuItem value="" disabled>
            {t("versionDiff.fromVersion")}
          </MenuItem>
          {versions.map((v) => (
            <MenuItem key={v.id} value={v.id}>
              v{v.version_number}
            </MenuItem>
          ))}
        </Select>
        <Typography variant="body2">{t("versionDiff.to")}</Typography>
        <Select
          size="small"
          value={vbId}
          onChange={(e) => setVbId(e.target.value)}
          displayEmpty
          sx={{ minWidth: 120 }}
        >
          <MenuItem value="" disabled>
            {t("versionDiff.toVersion")}
          </MenuItem>
          {versions.map((v) => (
            <MenuItem key={v.id} value={v.id}>
              v{v.version_number}
            </MenuItem>
          ))}
        </Select>
        {totalChanges !== null && (
          <Chip
            label={totalChanges !== 1 ? t("versionDiff.changesPlural", { count: String(totalChanges) }) : t("versionDiff.changes", { count: String(totalChanges) })}
            size="small"
          />
        )}
      </Box>

      {diff.data &&
        Object.entries(diff.data.diff).map(([cat, items]) => {
          const total =
            items.added.length + items.removed.length + items.changed.length;
          if (total === 0) return null;
          return (
            <Box key={cat} mb={2}>
              <Typography variant="caption" fontWeight={700} color="text.secondary">
                {CATEGORY_LABELS[cat] ?? cat}
              </Typography>
              <Divider sx={{ mb: 0.5 }} />
              {items.added.map((item, i) => (
                <Box key={i} display="flex" alignItems="center" gap={0.5} py={0.25}>
                  <AddCircleOutlineIcon fontSize="small" color="success" />
                  <Typography variant="body2">
                    {String(item.alias ?? item.slug ?? item.name ?? item.id)}
                  </Typography>
                </Box>
              ))}
              {items.removed.map((item, i) => (
                <Box key={i} display="flex" alignItems="center" gap={0.5} py={0.25}>
                  <RemoveCircleOutlineIcon fontSize="small" color="error" />
                  <Typography
                    variant="body2"
                    sx={{ textDecoration: "line-through", color: "text.secondary" }}
                  >
                    {String(item.alias ?? item.slug ?? item.name ?? item.id)}
                  </Typography>
                </Box>
              ))}
              {items.changed.map((item, i) => (
                <Box key={i} mb={0.5}>
                  <Box display="flex" alignItems="center" gap={0.5}>
                    <EditIcon fontSize="small" color="warning" />
                    <Typography variant="body2">
                      {String(
                        (item as Record<string, unknown>).alias ??
                          (item as Record<string, unknown>).slug ??
                          item.id,
                      )}
                    </Typography>
                  </Box>
                  <Table size="small" sx={{ ml: 2.5 }}>
                    <TableHead>
                      <TableRow>
                        <TableCell sx={{ py: 0.25 }}>{t("versionDiff.field")}</TableCell>
                        <TableCell sx={{ py: 0.25 }}>{t("versionDiff.before")}</TableCell>
                        <TableCell sx={{ py: 0.25 }}>{t("versionDiff.after")}</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {Object.entries(item.changes).map(([field, change]) => (
                        <TableRow key={field}>
                          <TableCell sx={{ py: 0.25 }}>{field}</TableCell>
                          <TableCell sx={{ py: 0.25, color: "error.main" }}>
                            {JSON.stringify(change.from)}
                          </TableCell>
                          <TableCell sx={{ py: 0.25, color: "success.main" }}>
                            {JSON.stringify(change.to)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </Box>
              ))}
            </Box>
          );
        })}

      {diff.data && totalChanges === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("versionDiff.noDifferences")}
        </Typography>
      )}
      {diff.isLoading && (
        <Typography variant="body2" color="text.secondary">
          {t("versionDiff.loadingDiff")}
        </Typography>
      )}
      {diff.isError && (
        <Typography variant="body2" color="error.main">
          {t("versionDiff.failedToLoadDiff")}
        </Typography>
      )}
    </Box>
  );
}
