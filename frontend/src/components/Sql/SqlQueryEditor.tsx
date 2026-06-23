/**
 * SqlQueryEditor — shared SQL textarea + dialect picker + action row.
 *
 * No API calls, no routing. Callers pass handlers for Validate / Dry run /
 * Save / Execute, and the component reports its internal state back.
 *
 * Reused by QueryPanel (semantic/query-router flow) and PocketDrawer
 * (pocket-defining-SQL flow).
 */
import {
  Box,
  Button,
  CircularProgress,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Tooltip,
} from "@mui/material";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import RuleIcon from "@mui/icons-material/Rule";
import SaveIcon from "@mui/icons-material/Save";
import VisibilityIcon from "@mui/icons-material/Visibility";
import { useT } from "../../i18n";

export interface SqlDialect {
  value: string;
  label: string;
}

export interface SqlQueryEditorProps {
  sql: string;
  onSqlChange: (next: string) => void;
  dialect?: string;
  onDialectChange?: (next: string) => void;
  dialects?: readonly SqlDialect[];
  onValidate?: () => void;
  onDryRun?: () => void;
  onExecute?: () => void;
  onSave?: () => void;
  validating?: boolean;
  dryRunning?: boolean;
  executing?: boolean;
  saving?: boolean;
  executeDisabled?: boolean;
  executeDisabledReason?: string;
  readOnly?: boolean;
  minHeight?: number;
  placeholder?: string;
}

function formatSql(sql: string): string {
  if (!sql.trim()) return sql;
  const breakBefore =
    /\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|JOIN|LEFT JOIN|RIGHT JOIN|INNER JOIN|FULL JOIN|UNION|UNION ALL|ON)\b/gi;
  let out = sql.replace(/\s+/g, " ").trim();
  out = out.replace(breakBefore, (m) => `\n${m.toUpperCase()}`);
  out = out.replace(/\nON\b/gi, "\n  ON");
  return out.trim();
}

export default function SqlQueryEditor(props: SqlQueryEditorProps) {
  const t = useT();
  const {
    sql,
    onSqlChange,
    dialect,
    onDialectChange,
    dialects,
    onValidate,
    onDryRun,
    onExecute,
    onSave,
    validating = false,
    dryRunning = false,
    executing = false,
    saving = false,
    executeDisabled = false,
    executeDisabledReason,
    readOnly = false,
    minHeight = 140,
    placeholder = t("sqlEditor.placeholder"),
  } = props;

  const showDialect = Boolean(dialects && dialects.length > 0 && dialect !== undefined);
  const busy = validating || dryRunning || executing || saving;
  const disabledByEmpty = !sql.trim();

  return (
    <Stack spacing={1.5}>
      <Box>
        <Box display="flex" alignItems="center" gap={1} mb={0.5}>
          {showDialect && (
            <FormControl size="small" sx={{ minWidth: 160 }}>
              <InputLabel>{t("sqlEditor.dialectLabel")}</InputLabel>
              <Select
                value={dialect}
                label={t("sqlEditor.dialectLabel")}
                onChange={(e) => onDialectChange?.(String(e.target.value))}
                disabled={readOnly}
              >
                {dialects!.map((d) => (
                  <MenuItem key={d.value} value={d.value}>
                    {d.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          <Box flexGrow={1} />
          <Tooltip title={t("sqlEditor.formatTooltip")}>
            <span>
              <IconButton
                size="small"
                onClick={() => onSqlChange(formatSql(sql))}
                disabled={readOnly || disabledByEmpty}
              >
                <AutoFixHighIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
          <Tooltip title={t("sqlEditor.copySqlTooltip")}>
            <span>
              <IconButton
                size="small"
                onClick={() => void navigator.clipboard?.writeText(sql)}
                disabled={disabledByEmpty}
              >
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Box>
        <Paper
          variant="outlined"
          sx={{
            p: 0,
            "& textarea": {
              fontFamily: "JetBrains Mono, monospace",
              fontSize: 12,
              padding: "8px",
              border: "none",
              outline: "none",
              width: "100%",
              minHeight,
              resize: "vertical",
              boxSizing: "border-box",
              backgroundColor: "transparent",
              color: "#212121",
            },
          }}
        >
          <textarea
            value={sql}
            onChange={(e) => onSqlChange(e.target.value)}
            spellCheck={false}
            placeholder={placeholder}
            readOnly={readOnly}
          />
        </Paper>
      </Box>

      <Stack direction="row" spacing={1} flexWrap="wrap">
        {onValidate && (
          <Button
            variant="outlined"
            size="small"
            startIcon={validating ? <CircularProgress size={14} /> : <RuleIcon />}
            onClick={onValidate}
            disabled={busy || disabledByEmpty}
          >
            {t("sqlEditor.validateButton")}
          </Button>
        )}
        {onDryRun && (
          <Button
            variant="outlined"
            size="small"
            startIcon={dryRunning ? <CircularProgress size={14} /> : <VisibilityIcon />}
            onClick={onDryRun}
            disabled={busy || disabledByEmpty}
          >
            {t("sqlEditor.dryRunButton")}
          </Button>
        )}
        {onExecute && (
          <Tooltip title={executeDisabled ? executeDisabledReason ?? "" : ""}>
            <span>
              <Button
                variant="contained"
                size="small"
                startIcon={
                  executing ? <CircularProgress size={14} color="inherit" /> : <PlayArrowIcon />
                }
                onClick={onExecute}
                disabled={busy || disabledByEmpty || executeDisabled}
              >
                {t("sqlEditor.executeButton")}
              </Button>
            </span>
          </Tooltip>
        )}
        {onSave && (
          <Button
            variant="contained"
            size="small"
            startIcon={saving ? <CircularProgress size={14} color="inherit" /> : <SaveIcon />}
            onClick={onSave}
            disabled={busy || disabledByEmpty}
          >
            {t("sqlEditor.saveButton")}
          </Button>
        )}
      </Stack>
    </Stack>
  );
}

export { formatSql };
