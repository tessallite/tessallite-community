import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
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
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import RemoveIcon from "@mui/icons-material/Remove";

export interface RenamePreviewRow {
  type: "dimension" | "measure";
  id: string;
  source_column_name: string;
  current_name: string;    // empty string in "new-table" mode
  suggested_name: string;
}

interface Props {
  open: boolean;
  /** alias-change: renaming existing attrs after alias edit.
   *  new-table: preview before classification save. */
  mode: "alias-change" | "new-table";
  rows: RenamePreviewRow[];
  /** Model-wide attr names excluding those being renamed. All lowercase. */
  takenNames: Set<string>;
  applying?: boolean;
  onApply: (final: Array<{ type: string; id: string; name: string }>) => void;
  /** Keep current names as-is; no rename applied. alias-change mode only. */
  onKeep: () => void;
  /** Revert alias to old value (alias-change) or cancel save (new-table). */
  onRevert: () => void;
}

export default function AttributeRenameDialog({
  open,
  mode,
  rows,
  takenNames,
  applying = false,
  onApply,
  onKeep,
  onRevert,
}: Props) {
  // Editable name per row, keyed by row id.
  const [values, setValues] = useState<Record<string, string>>({});

  // Re-seed whenever rows change (dialog re-opened with different data).
  useEffect(() => {
    setValues(Object.fromEntries(rows.map((r) => [r.id, r.suggested_name])));
  }, [rows]);

  // Compute per-row errors, processing in order so duplicates within the
  // dialog are caught on the second occurrence.
  const errors = useMemo<Record<string, string | null>>(() => {
    const result: Record<string, string | null> = {};
    const seen = new Set<string>(takenNames);
    for (const row of rows) {
      const val = (values[row.id] ?? "").trim();
      if (!val) {
        result[row.id] = "Name is required.";
      } else if (seen.has(val.toLowerCase())) {
        result[row.id] = "Already in use.";
      } else {
        result[row.id] = null;
        seen.add(val.toLowerCase());
      }
    }
    return result;
  }, [values, takenNames, rows]);

  const hasErrors = Object.values(errors).some((e) => e !== null);

  function handleApply() {
    const final = rows.map((r) => ({
      type: r.type,
      id: r.id,
      name: (values[r.id] ?? r.suggested_name).trim(),
    }));
    onApply(final);
  }

  const title =
    mode === "alias-change"
      ? "Review attribute renames"
      : "Confirm attribute names before saving";

  const subtitle =
    mode === "alias-change"
      ? "The alias change affects the names of the following attributes. Edit proposed names or keep current ones."
      : "The following column names conflict with existing attributes. Adjust names before saving.";

  return (
    <Dialog open={open} maxWidth="md" fullWidth>
      <DialogTitle>{title}</DialogTitle>
      <DialogContent dividers>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {subtitle}
        </Typography>

        {rows.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No renames required.
          </Typography>
        ) : (
          <TableContainer
            sx={{ border: 1, borderColor: "divider", borderRadius: 1, maxHeight: 400 }}
          >
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 90 }}>
                    Type
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                    Source column
                  </TableCell>
                  {mode === "alias-change" && (
                    <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                      Current name
                    </TableCell>
                  )}
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                    New name
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 36 }} />
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((row) => {
                  const val = values[row.id] ?? "";
                  const err = errors[row.id];
                  const unchanged = mode === "alias-change" && val.trim() === row.current_name;
                  return (
                    <TableRow key={row.id}>
                      <TableCell sx={{ fontSize: "0.78rem", color: "text.secondary" }}>
                        {row.type}
                      </TableCell>
                      <TableCell sx={{ fontFamily: "monospace", fontSize: "0.78rem" }}>
                        {row.source_column_name}
                      </TableCell>
                      {mode === "alias-change" && (
                        <TableCell sx={{ fontFamily: "monospace", fontSize: "0.78rem", color: "text.secondary" }}>
                          {row.current_name}
                        </TableCell>
                      )}
                      <TableCell sx={{ py: 0.5 }}>
                        <TextField
                          size="small"
                          fullWidth
                          value={val}
                          onChange={(e) =>
                            setValues((prev) => ({ ...prev, [row.id]: e.target.value }))
                          }
                          error={!!err}
                          helperText={err ?? undefined}
                          inputProps={{ style: { fontFamily: "monospace", fontSize: "0.82rem" } }}
                          sx={{ minWidth: 180 }}
                        />
                      </TableCell>
                      <TableCell align="center" sx={{ py: 0.5 }}>
                        {unchanged ? (
                          <Tooltip title="No change from current name">
                            <RemoveIcon sx={{ fontSize: 16, color: "text.disabled" }} />
                          </Tooltip>
                        ) : err ? (
                          <Tooltip title={err}>
                            <ErrorOutlineIcon sx={{ fontSize: 16, color: "error.main" }} />
                          </Tooltip>
                        ) : (
                          <Tooltip title="Valid">
                            <CheckCircleOutlineIcon sx={{ fontSize: 16, color: "success.main" }} />
                          </Tooltip>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        )}

        {mode === "alias-change" && (
          <Alert severity="info" sx={{ mt: 2 }}>
            Renamed attributes take effect in connected BI tools after you redeploy the model.
            Saved queries that reference the old names by string will need to be updated manually.
          </Alert>
        )}
      </DialogContent>

      <DialogActions sx={{ gap: 1, px: 3, py: 2 }}>
        <Button
          variant="outlined"
          color="error"
          size="small"
          onClick={onRevert}
          disabled={applying}
        >
          {mode === "alias-change" ? "Revert alias change" : "Cancel"}
        </Button>

        <Box sx={{ flex: 1 }} />

        {mode === "alias-change" && (
          <Button
            variant="text"
            size="small"
            onClick={onKeep}
            disabled={applying}
          >
            Keep current names
          </Button>
        )}

        <Button
          variant="contained"
          size="small"
          onClick={handleApply}
          disabled={hasErrors || applying}
        >
          {applying ? <CircularProgress size={16} sx={{ mr: 1 }} /> : null}
          Apply renames
        </Button>
      </DialogActions>
    </Dialog>
  );
}
