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
import { countDiffChanges, isSingletonDiff, useVersionDiff } from "../../api/versionsApi";
import type { DiffMap, VersionItem } from "../../api/versionsApi";

/**
 * Labels for every snapshot category the backend may include in a diff response
 * (Bug-5654: unlabelled categories previously fell back to the raw key string).
 * Exposed as a hook so any diff surface — the version A/B diff AND the
 * pending-change review (G-013-01) — renders category names identically.
 */
export function useDiffCategoryLabels(): Record<string, string> {
  const t = useT();
  return {
    tables: t("versionDiff.categoryTables"),
    columns: t("versionDiff.categoryColumns"),
    user_defined_attributes: t("versionDiff.categoryUserDefinedAttributes"),
    uda_column_refs: t("versionDiff.categoryUdaColumnRefs"),
    joins: t("versionDiff.categoryJoins"),
    hierarchies: t("versionDiff.categoryHierarchies"),
    dimensions: t("versionDiff.categoryDimensions"),
    measures: t("versionDiff.categoryMeasures"),
    named_sets: t("versionDiff.categoryNamedSets"),
    named_queries: t("versionDiff.categoryNamedQueries"),
    attribute_relationships: t("versionDiff.categoryAttributeRelationships"),
    kpis: t("versionDiff.categoryKpis"),
    drill_through_sets: t("versionDiff.categoryDrillThroughSets"),
    data_sources: t("versionDiff.categoryDataSources"),
    data_targets: t("versionDiff.categoryDataTargets"),
    calendar_tables: t("versionDiff.categoryCalendarTables"),
    lineage_mappings: t("versionDiff.categoryLineageMappings"),
    aggregates: t("versionDiff.categoryAggregates"),
    personas: t("versionDiff.categoryPersonas"),
    data_tags: t("versionDiff.categoryDataTags"),
    persona_tag_restrictions: t("versionDiff.categoryPersonaTagRestrictions"),
    pockets: t("versionDiff.categoryPockets"),
    row_security_rules: t("versionDiff.categoryRowSecurityRules"),
    glossary_entries: t("versionDiff.categoryGlossaryEntries"),
    aggregate_lifecycle_events: t("versionDiff.categoryAggregateLifecycleEvents"),
    source_statistics: t("versionDiff.categorySourceStatistics"),
    source_join_statistics: t("versionDiff.categorySourceJoinStatistics"),
    model_parameters: t("versionDiff.categoryModelParameters"),
    data_quality_rules: t("versionDiff.categoryDataQualityRules"),
    entity_translations: t("versionDiff.categoryEntityTranslations"),
    // Bug-5916: singleton/dict snapshot categories.
    model: t("versionDiff.categoryModel"),
    model_alias_map: t("versionDiff.categoryModelAliasMap"),
    refresh_sla_config: t("versionDiff.categoryRefreshSlaConfig"),
    ai_scheduler_config: t("versionDiff.categoryAiSchedulerConfig"),
    model_settings: t("versionDiff.categoryModelSettings"),
  };
}

/**
 * Renders the added / removed / changed rows of a diff map. This is the single
 * presentation of a ``diff_snapshots`` payload, shared by the version A/B diff
 * and the pending-change review so both read identically (no second differ, no
 * second renderer).
 */
export function DiffBody({
  diff,
  labels,
}: {
  diff: DiffMap;
  labels: Record<string, string>;
}) {
  const t = useT();
  const total = countDiffChanges(diff);
  if (total === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        {t("versionDiff.noDifferences")}
      </Typography>
    );
  }
  return (
    <>
      {Object.entries(diff).map(([cat, entry]) => {
        if (isSingletonDiff(entry)) {
          const fields = Object.entries(entry.changes);
          if (fields.length === 0) return null;
          return (
            <Box key={cat} mb={2}>
              <Typography variant="caption" fontWeight={700} color="text.secondary">
                {labels[cat] ?? cat}
              </Typography>
              <Divider sx={{ mb: 0.5 }} />
              <Box mb={0.5}>
                <Box display="flex" alignItems="center" gap={0.5}>
                  <EditIcon fontSize="small" color="warning" />
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
                    {fields.map(([field, change]) => (
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
            </Box>
          );
        }
        const items = entry;
        const catTotal =
          items.added.length + items.removed.length + items.changed.length;
        if (catTotal === 0) return null;
        return (
          <Box key={cat} mb={2}>
            <Typography variant="caption" fontWeight={700} color="text.secondary">
              {labels[cat] ?? cat}
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
    </>
  );
}

interface Props {
  projectId: string;
  modelId: string;
  versions: VersionItem[];
}

export default function VersionDiffPanel({ projectId, modelId, versions }: Props) {
  const t = useT();
  const labels = useDiffCategoryLabels();
  const [vaId, setVaId] = useState<string>(versions[1]?.id ?? "");
  const [vbId, setVbId] = useState<string>(versions[0]?.id ?? "");
  const diff = useVersionDiff(projectId, modelId, vaId || undefined, vbId || undefined);

  const totalChanges = diff.data ? countDiffChanges(diff.data.diff) : null;

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

      {diff.data && <DiffBody diff={diff.data.diff} labels={labels} />}
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
