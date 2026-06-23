import { useState } from "react";
import { useT } from "../../i18n";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  MenuItem,
  Select,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import NavigateBeforeIcon from "@mui/icons-material/NavigateBefore";
import NavigateNextIcon from "@mui/icons-material/NavigateNext";
import { modelTablesApi } from "../../api/client";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
  tableId: string;
  tableName: string;
}

const PAGE_SIZES = [25, 50, 100];

export default function DataPreviewPanel({
  open,
  onClose,
  projectId,
  modelId,
  tableId,
  tableName,
}: Props) {
  const t = useT();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);

  const preview = useQuery({
    queryKey: ["table-preview", projectId, modelId, tableId, page, pageSize],
    queryFn: () =>
      modelTablesApi.preview(projectId, modelId, tableId, page, pageSize, page === 0),
    enabled: open,
  });

  const totalRows = preview.data?.total_rows;
  const columns = preview.data?.columns ?? [];
  const rows = preview.data?.rows ?? [];
  const hasMore = preview.data?.has_more ?? false;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle>
        <Box display="flex" alignItems="center" justifyContent="space-between">
          <Typography variant="subtitle1" fontWeight={700}>
            {t("dataPreview.title", { tableName })}
          </Typography>
          <IconButton size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Box>
      </DialogTitle>
      <DialogContent>
        {preview.isLoading && (
          <Box display="flex" justifyContent="center" py={4}>
            <CircularProgress />
          </Box>
        )}

        {preview.isError && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {t("dataPreview.failedToLoad")}{" "}
            <Button size="small" onClick={() => preview.refetch()}>
              {t("dataPreview.retry")}
            </Button>
          </Alert>
        )}

        {preview.isSuccess && (
          <>
            <Box display="flex" alignItems="center" gap={2} mb={1}>
              {totalRows != null && (
                <Typography variant="caption" color="text.secondary">
                  {t("dataPreview.totalRows", { count: totalRows.toLocaleString() })}
                </Typography>
              )}
              <Box flexGrow={1} />
              <Typography variant="caption" color="text.secondary">
                {t("dataPreview.rowsPerPage")}
              </Typography>
              <FormControl size="small" sx={{ minWidth: 70 }}>
                <Select
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(Number(e.target.value));
                    setPage(0);
                  }}
                  sx={{ height: 28, fontSize: 12 }}
                >
                  {PAGE_SIZES.map((s) => (
                    <MenuItem key={s} value={s}>
                      {s}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <IconButton
                size="small"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                <NavigateBeforeIcon fontSize="small" />
              </IconButton>
              <Typography variant="caption">{t("dataPreview.pageLabel", { page: String(page + 1) })}</Typography>
              <IconButton
                size="small"
                disabled={!hasMore}
                onClick={() => setPage((p) => p + 1)}
              >
                <NavigateNextIcon fontSize="small" />
              </IconButton>
            </Box>

            <TableContainer sx={{ maxHeight: 480, overflow: "auto" }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    {columns.map((col) => (
                      <TableCell
                        key={col}
                        sx={{
                          fontWeight: 600,
                          fontSize: 12,
                          fontFamily: "monospace",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {col}
                      </TableCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {rows.map((row, idx) => (
                    <TableRow key={idx} hover>
                      {columns.map((col) => (
                        <TableCell
                          key={col}
                          sx={{
                            fontSize: 12,
                            whiteSpace: "nowrap",
                            maxWidth: 300,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                          }}
                        >
                          {row[col] == null ? (
                            <Typography
                              variant="caption"
                              sx={{ color: "text.disabled", fontStyle: "italic" }}
                            >
                              {t("common.nullValue")}
                            </Typography>
                          ) : (
                            String(row[col])
                          )}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                  {rows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={columns.length || 1} align="center">
                        <Typography variant="body2" color="text.secondary">
                          {t("dataPreview.noData")}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableContainer>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
