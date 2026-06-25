import { useQuery } from "@tanstack/react-query";
import {
  Box,
  CircularProgress,
  Divider,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import { tableAttributesApi } from "../../../api/client";
import type { ModelTable } from "../../../api/types";
import GeneralTab from "./GeneralTab";
import { useT } from "../../../i18n";

interface Props {
  projectId: string;
  modelId: string;
  sourceId: string;
  table: ModelTable;
  connectionId: string | null;
}

export default function DetailsTab({ projectId, modelId, sourceId, table }: Props) {
  const t = useT();

  const attributes = useQuery({
    queryKey: ["tableAttributes", projectId, modelId, table.id],
    queryFn: () => tableAttributesApi.list(projectId, modelId, table.id),
    staleTime: 20 * 1000,
  });

  return (
    <Stack spacing={2} sx={{ mt: 0.5 }}>
      <GeneralTab
        projectId={projectId}
        modelId={modelId}
        sourceId={sourceId}
        table={table}
      />

      <Divider />

      <Box>
        <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
          {t("tableEditDetails.tableAttrsTitle")}
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block" mb={1}>
          {t("tableEditDetails.tableAttrsDesc")}
        </Typography>

        {attributes.isLoading && (
          <Box display="flex" alignItems="center" gap={1} py={1}>
            <CircularProgress size={14} />
            <Typography variant="caption" color="text.secondary">{t("tableEditDetails.loading")}</Typography>
          </Box>
        )}

        {attributes.isError && (
          <Typography variant="caption" color="error">
            {t("tableEditDetails.loadError")}
          </Typography>
        )}

        {attributes.data && attributes.data.length === 0 && (
          <Typography variant="caption" color="text.secondary">
            {t("tableEditDetails.noAttributes")}
          </Typography>
        )}

        {attributes.data && attributes.data.length > 0 && (
          <TableContainer
            sx={{ border: 1, borderColor: "divider", borderRadius: 1, maxHeight: 320 }}
          >
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                    {t("tableEditDetails.nameHeader")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 100 }}>
                    {t("tableEditDetails.dataTypeHeader")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 80 }}>
                    {t("tableEditDetails.kindHeader")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                    {t("tableEditDetails.displayNameHeader")}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {attributes.data.map((attr) => (
                  <TableRow
                    key={attr.id}
                    sx={{
                      opacity: attr.is_hidden ? 0.5 : 1,
                      "&:hover": { bgcolor: "action.hover" },
                    }}
                  >
                    <TableCell sx={{ py: 0.5 }}>
                      <Box display="flex" alignItems="center" gap={0.5}>
                        <Typography
                          variant="body2"
                          sx={{
                            fontFamily: "monospace",
                            fontSize: "0.8rem",
                            textDecoration: attr.is_hidden ? "line-through" : "none",
                            color: attr.is_user_defined ? "secondary.main" : "text.primary",
                            fontStyle: attr.is_user_defined ? "italic" : "normal",
                          }}
                        >
                          {attr.is_user_defined ? t("tableEditDetails.computedPrefix") : ""}{attr.name}
                        </Typography>
                        {attr.is_hidden && (
                          <VisibilityOffIcon
                            sx={{ fontSize: 12, color: "text.disabled" }}
                          />
                        )}
                      </Box>
                    </TableCell>
                    <TableCell
                      sx={{
                        fontSize: "0.75rem",
                        color: "text.secondary",
                        py: 0.5,
                      }}
                    >
                      {attr.data_type}
                    </TableCell>
                    <TableCell
                      sx={{
                        fontSize: "0.75rem",
                        color: attr.is_user_defined ? "secondary.main" : "text.secondary",
                        py: 0.5,
                      }}
                    >
                      {attr.is_user_defined ? t("tableEditDetails.kindComputed") : t("tableEditDetails.kindPhysical")}
                    </TableCell>
                    <TableCell
                      sx={{
                        fontSize: "0.8rem",
                        color: attr.display_name ? "text.primary" : "text.disabled",
                        py: 0.5,
                      }}
                    >
                      {attr.display_name ?? t("common.na")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Box>
    </Stack>
  );
}
