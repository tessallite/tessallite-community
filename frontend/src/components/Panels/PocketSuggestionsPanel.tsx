import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  FormControl,
  InputLabel,
  LinearProgress,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import {
  optimizerApiClient,
  type PocketSuggestionItem,
} from "../../api/client";

interface Props {
  modelId: string;
  // F-005-13: act on a suggestion — open the create drawer prefilled with its
  // model-subset SQL. Undefined when the viewer cannot manage pockets.
  onCreate?: (definingSql: string) => void;
}

function fmtBytes(b: number): string {
  if (b >= 1_073_741_824) return `${(b / 1_073_741_824).toFixed(1)} GB`;
  if (b >= 1_048_576) return `${(b / 1_048_576).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

function predicateSummary(
  predicates: PocketSuggestionItem["predicates"],
  nullLabel: string,
): string {
  return predicates
    .map((p) => {
      const val =
        Array.isArray(p.value) ? `(${p.value.join(", ")})` : String(p.value ?? nullLabel);
      const op = p.operator === "eq" ? "=" : p.operator;
      return `${p.column_name} ${op} ${val}`;
    })
    .join(" AND ");
}

export default function PocketSuggestionsPanel({ modelId, onCreate }: Props) {
  const t = useT();
  const [days, setDays] = useState(30);

  const { data, isLoading, error } = useQuery({
    queryKey: ["pocket-suggestions", modelId, days],
    queryFn: () => optimizerApiClient.getPocketSuggestions(modelId, days),
    enabled: Boolean(modelId),
  });

  if (isLoading) {
    return (
      <Box sx={{ p: 3, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  if (error) {
    return (
      <Alert severity="error" sx={{ m: 2 }}>
        {t("pocketTables.suggestions.failedToLoad", { error: (error as Error).message })}
      </Alert>
    );
  }

  const suggested = (data?.suggestions ?? []).filter((s) => s.status === "suggested");
  const hinted = (data?.suggestions ?? []).filter((s) => s.status === "hinted");
  const budgetBytes = data?.budget_bytes ?? null;
  const usedBytes = data?.used_bytes ?? 0;
  const budgetPct =
    budgetBytes != null && budgetBytes > 0
      ? Math.min((usedBytes / budgetBytes) * 100, 100)
      : null;

  return (
    <Box>
      <Box display="flex" alignItems="center" mb={2} gap={2}>
        <Typography variant="subtitle1" fontWeight={600} flex={1}>
          {t("pocketTables.suggestions.title")}
        </Typography>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>{t("pocketTables.suggestions.lookback")}</InputLabel>
          <Select
            value={days}
            label={t("pocketTables.suggestions.lookback")}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            <MenuItem value={7}>{t("pocketTables.suggestions.last7")}</MenuItem>
            <MenuItem value={30}>{t("pocketTables.suggestions.last30")}</MenuItem>
            <MenuItem value={90}>{t("pocketTables.suggestions.last90")}</MenuItem>
          </Select>
        </FormControl>
      </Box>

      {/* Budget indicator */}
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="body2" fontWeight={600} gutterBottom>
          {t("pocketTables.suggestions.budgetTitle")}
        </Typography>
        {budgetBytes != null ? (
          <Stack spacing={1}>
            <Box display="flex" justifyContent="space-between">
              <Typography variant="caption" color="text.secondary">
                {t("pocketTables.suggestions.budgetUsed", { used: fmtBytes(usedBytes), limit: fmtBytes(budgetBytes) })}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t("pocketTables.suggestions.budgetRemaining", { remaining: fmtBytes(data?.remaining_bytes ?? 0) })}
              </Typography>
            </Box>
            <LinearProgress
              variant="determinate"
              value={budgetPct ?? 0}
              sx={{
                height: 8,
                borderRadius: 1,
                bgcolor: "grey.200",
                "& .MuiLinearProgress-bar": {
                  bgcolor: (budgetPct ?? 0) > 90 ? "error.main" : "primary.main",
                },
              }}
            />
          </Stack>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {t("pocketTables.suggestions.noBudget")}
          </Typography>
        )}
      </Paper>

      {/* Suggested pockets */}
      {suggested.length > 0 && (
        <>
          <Typography
            variant="subtitle2"
            fontWeight={600}
            color="text.secondary"
            sx={{ mt: 2, mb: 1 }}
          >
            {t("pocketTables.suggestions.suggestedCount", { count: String(suggested.length) })}
          </Typography>
          <SuggestionTable items={suggested} t={t} onCreate={onCreate} />
        </>
      )}

      {/* Hinted pockets */}
      {hinted.length > 0 && (
        <>
          <Typography
            variant="subtitle2"
            fontWeight={600}
            color="text.secondary"
            sx={{ mt: 2, mb: 1 }}
          >
            {t("pocketTables.suggestions.potentialCount", { count: String(hinted.length) })}
          </Typography>
          <SuggestionTable items={hinted} t={t} onCreate={onCreate} />
        </>
      )}

      {suggested.length === 0 && hinted.length === 0 && (
        <Paper variant="outlined" sx={{ p: 3, textAlign: "center" }}>
          <Typography variant="body2" color="text.secondary">
            {t("pocketTables.suggestions.noSuggestions")}
          </Typography>
        </Paper>
      )}
    </Box>
  );
}

interface SuggestionTableProps {
  items: PocketSuggestionItem[];
  t: ReturnType<typeof useT>;
  onCreate?: (definingSql: string) => void;
}

function SuggestionTable({ items, t, onCreate }: SuggestionTableProps) {
  return (
    <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
      <Table size="small">
        <TableHead>
          <TableRow sx={{ bgcolor: "grey.50" }}>
            <TableCell>{t("pocketTables.suggestions.ordinalLabel")}</TableCell>
            <TableCell>{t("pocketTables.suggestions.tablePredicates")}</TableCell>
            <TableCell align="right">{t("pocketTables.suggestions.tableHits")}</TableCell>
            <TableCell align="right">{t("pocketTables.suggestions.tableSize")}</TableCell>
            <TableCell align="right">{t("pocketTables.suggestions.tableScore")}</TableCell>
            <TableCell>{t("pocketTables.suggestions.tableStatus")}</TableCell>
            {onCreate && <TableCell align="right">{t("pocketTables.suggestions.tableAction")}</TableCell>}
          </TableRow>
        </TableHead>
        <TableBody>
          {items.map((item, i) => (
            <TableRow key={item.predicate_set_hash}>
              <TableCell>{i + 1}</TableCell>
              <TableCell>
                <Typography
                  variant="body2"
                  sx={{ fontFamily: "monospace", fontSize: 12 }}
                >
                  {predicateSummary(item.predicates, t("common.nullValue"))}
                </Typography>
                {item.sample_measures.length > 0 && (
                  <Typography variant="caption" color="text.secondary" display="block">
                    {t("pocketTables.suggestions.measuresLabel")}: {item.sample_measures.slice(0, 3).join(", ")}
                    {item.sample_measures.length > 3 && ` +${item.sample_measures.length - 3}`}
                  </Typography>
                )}
              </TableCell>
              <TableCell align="right">{item.hit_count}</TableCell>
              <TableCell align="right">{fmtBytes(item.estimated_size_bytes)}</TableCell>
              <TableCell align="right">{item.score.toFixed(1)}</TableCell>
              <TableCell>
                <Chip
                  size="small"
                  label={item.status === "suggested" ? t("pocketTables.suggestions.suggested") : t("pocketTables.suggestions.overBudget")}
                  color={item.status === "suggested" ? "success" : "warning"}
                  variant="outlined"
                  sx={{ fontSize: 11 }}
                />
              </TableCell>
              {onCreate && (
                <TableCell align="right">
                  <Button
                    size="small"
                    startIcon={<AddIcon />}
                    onClick={() => onCreate(item.defining_sql)}
                    disabled={!item.defining_sql}
                  >
                    {t("pocketTables.suggestions.createAction")}
                  </Button>
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
