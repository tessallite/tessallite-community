import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Collapse,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { kpisApi, namedSetsApi } from "../../api/client";
import type { EntityUsageEntry } from "../../api/types";
import { useT } from "../../i18n";

interface EntityImpactSummaryProps {
  entityType: "named_set" | "kpi";
  entityId: string;
  entityName: string;
  projectId: string;
  modelId: string;
}

export default function EntityImpactSummary({
  entityType,
  entityId,
  entityName,
  projectId,
  modelId,
}: EntityImpactSummaryProps) {
  const t = useT();
  const [usage, setUsage] = useState<EntityUsageEntry[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    const fetchUsage =
      entityType === "kpi" ? kpisApi.listUsage : namedSetsApi.listUsage;
    fetchUsage(projectId, modelId, entityId)
      .then((data) => {
        if (!cancelled) setUsage(data);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entityType, entityId, projectId, modelId]);

  if (loading) {
    return (
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, my: 1 }}>
        <CircularProgress size={14} />
        <Typography variant="caption" color="text.secondary">
          {t("entityImpact.checkingUsage")}
        </Typography>
      </Box>
    );
  }

  if (error || !usage) return null;
  if (usage.length === 0) return null;

  const workbookIds = new Set(
    usage.filter((u) => u.workbook_id).map((u) => u.workbook_id!),
  );
  const users = new Set(
    usage.filter((u) => u.reported_by).map((u) => u.reported_by!),
  );

  const label = entityType === "kpi" ? "KPI" : "named set";

  return (
    <Alert
      severity="warning"
      icon={<WarningAmberIcon fontSize="small" />}
      sx={{ my: 1 }}
      action={
        <Box
          sx={{ cursor: "pointer", display: "flex", alignItems: "center" }}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? (
            <ExpandLessIcon fontSize="small" />
          ) : (
            <ExpandMoreIcon fontSize="small" />
          )}
        </Box>
      }
    >
      <Typography variant="body2">
        {(() => {
          const wbOne = workbookIds.size === 1;
          const usrOne = users.size === 1;
          let key: string;
          if (wbOne && usrOne) key = "entityImpact.referencedInSingularSingular";
          else if (wbOne) key = "entityImpact.referencedInSingularPlural";
          else if (usrOne) key = "entityImpact.referencedInPluralSingular";
          else key = "entityImpact.referencedInPluralPlural";
          const params: Record<string, string> = { label };
          if (!wbOne) params.workbooks = String(workbookIds.size);
          if (!usrOne) params.users = String(users.size);
          return t(key, params);
        })()}
      </Typography>
      <Collapse in={expanded}>
        <Box sx={{ mt: 1 }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 700, fontSize: 11, py: 0.5 }}>
                  {t("entityImpact.worksheetHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 11, py: 0.5 }}>
                  {t("entityImpact.cellHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 11, py: 0.5 }}>
                  {t("entityImpact.userHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 11, py: 0.5 }}>
                  {t("entityImpact.typeHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 11, py: 0.5 }}>
                  {t("entityImpact.reportedHeader")}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {usage.map((u) => (
                <TableRow key={u.id}>
                  <TableCell sx={{ fontSize: 11, py: 0.25 }}>
                    {u.worksheet ?? "—"}
                  </TableCell>
                  <TableCell sx={{ fontSize: 11, py: 0.25, fontFamily: "monospace" }}>
                    {u.cell_reference ?? "—"}
                  </TableCell>
                  <TableCell sx={{ fontSize: 11, py: 0.25 }}>
                    {u.reported_by ?? "—"}
                  </TableCell>
                  <TableCell sx={{ fontSize: 11, py: 0.25 }}>
                    <Chip label={u.usage_type} size="small" variant="outlined" />
                  </TableCell>
                  <TableCell sx={{ fontSize: 11, py: 0.25 }}>
                    {new Date(u.reported_at).toLocaleDateString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      </Collapse>
    </Alert>
  );
}
