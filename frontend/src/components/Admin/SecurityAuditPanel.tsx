import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
  Button,
} from "@mui/material";
import NavigateNextIcon from "@mui/icons-material/NavigateNext";
import NavigateBeforeIcon from "@mui/icons-material/NavigateBefore";
import { securityAuditApi } from "../../api/client";
import { useT } from "../../i18n";

const PAGE_SIZE = 50;

export default function SecurityAuditPanel() {
  const t = useT();
  const [modelIdFilter, setModelIdFilter] = useState("");
  const [page, setPage] = useState(0);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["security-audit", modelIdFilter, page],
    queryFn: () =>
      securityAuditApi.list({
        model_id: modelIdFilter.trim() || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    refetchInterval: 60_000,
  });

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <Box>
      <Stack direction="row" spacing={2} mb={2} alignItems="center">
        <TextField
          label={t("audit.filterByModelId")}
          size="small"
          value={modelIdFilter}
          onChange={(e) => {
            setModelIdFilter(e.target.value);
            setPage(0);
          }}
          sx={{ width: 320 }}
          placeholder={t("audit.uuidPlaceholder")}
        />
        <Typography variant="body2" color="text.secondary" sx={{ ml: "auto" }}>
          {total} {t("audit.queriesWithSecurityRules")}
        </Typography>
      </Stack>

      {isLoading ? (
        <CircularProgress size={20} />
      ) : isError ? (
        // Bug-8145: a failed request used to fall through to the empty-state
        // message below, which tells the reviewer the opposite of the truth —
        // an unreadable log reads as "no queries with row security". Render a
        // distinct error state with retry instead (mirrors AuditLog.tsx).
        <Alert
          severity="error"
          action={
            <Button color="inherit" size="small" onClick={() => refetch()}>
              {t("auditLog.retry")}
            </Button>
          }
        >
          {t("audit.apiFailure")}
        </Alert>
      ) : items.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("audit.noQueriesWithRowSecurity")}
        </Typography>
      ) : (
        <>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t("audit.tableTime")}</TableCell>
                <TableCell>{t("audit.tableUser")}</TableCell>
                <TableCell>{t("audit.tableProtocol")}</TableCell>
                <TableCell>{t("audit.tableRoute")}</TableCell>
                <TableCell>{t("audit.tableRulesApplied")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((row) => (
                <TableRow key={row.query_log_id}>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    {new Date(row.created_at).toLocaleString()}
                  </TableCell>
                  <TableCell>{row.user_identity ?? t("common.na")}</TableCell>
                  <TableCell>{row.protocol}</TableCell>
                  <TableCell>{row.route_type}</TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap">
                      {row.security_rules_applied.map((r) => (
                        <Chip
                          key={r.rule_id}
                          label={r.rule_name}
                          size="small"
                          variant="outlined"
                          title={r.predicate_sql}
                        />
                      ))}
                    </Stack>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Stack direction="row" justifyContent="flex-end" spacing={1} mt={1}>
            <Button
              size="small"
              startIcon={<NavigateBeforeIcon />}
              disabled={page === 0}
              onClick={() => setPage((p) => p - 1)}
            >
              {t("audit.prevButton")}
            </Button>
            <Typography variant="body2" alignSelf="center">
              {t("audit.pageFormat", { page: String(page + 1), total: String(totalPages) })}
            </Typography>
            <Button
              size="small"
              endIcon={<NavigateNextIcon />}
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              {t("audit.nextButton")}
            </Button>
          </Stack>
        </>
      )}
    </Box>
  );
}
