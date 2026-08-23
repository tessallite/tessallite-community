import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Chip,
  IconButton,
  Stack,
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
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import { useT } from "../../../i18n";
import {
  useModel,
  useSources,
  useAllModelTables,
  useDimensions,
  useMeasures,
  useHierarchiesWithLevels,
  usePersonas,
} from "../../../api/hooks";
import {
  glossaryApi,
  queryRouterApiClient,
} from "../../../api/client";
import type {
  ModelTableWithAttributes,
  TableAttribute,
} from "../../../api/types_domains/sources_schema";
import PersonaPicker from "../../Persona/PersonaPicker";
import AttributeExportMenu from "./AttributeExportMenu";
import {
  beautifySql,
  buildAttributeRows,
  buildGlossaryIndex,
  buildModelSelectSql,
  resolvePersonaAttributeIds,
  selectableColumnNames,
  type AttributeRow,
  type ExportLabels,
} from "./attributeRows";

export default function ModelTab() {
  const t = useT();
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const pid = projectId ?? "";
  const mid = modelId ?? "";
  const [personaId, setPersonaId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const queryClient = useQueryClient();

  const model = useModel(pid, mid);
  const sources = useSources(pid, mid);
  const sourceIds = useMemo(
    () => (sources.data ?? []).map((s) => s.id),
    [sources.data],
  );
  const sortedSourceIds = useMemo(() => [...sourceIds].sort(), [sourceIds]);
  const tables = useAllModelTables(pid, mid, sourceIds);
  const dimensions = useDimensions(pid, mid);
  const measures = useMeasures(pid, mid);
  const hierarchies = useHierarchiesWithLevels(pid, mid);
  const personas = usePersonas(pid, mid, { forAudience: false });

  const tableList = useMemo(() => tables.data ?? [], [tables.data]);
  const batchRows = queryClient.getQueryData<ModelTableWithAttributes[]>([
    "allModelTables",
    pid,
    mid,
    sortedSourceIds,
  ]) ?? [];

  const glossary = useQuery({
    queryKey: ["glossary", pid, mid],
    queryFn: () => glossaryApi.list(pid, mid),
    enabled: !!pid && !!mid,
  });

  const attributesByTable = useMemo(() => {
    const map = new Map<string, TableAttribute[]>();
    for (const row of batchRows) {
      map.set(row.table.id, row.attributes);
    }
    for (const table of tableList) {
      if (!map.has(table.id)) {
        map.set(table.id, []);
      }
    }
    return map;
  }, [batchRows, tableList]);

  const selectedPersona = useMemo(
    () => (personas.data ?? []).find((p) => p.id === personaId) ?? null,
    [personas.data, personaId],
  );

  const visibleIds = useMemo(
    () =>
      resolvePersonaAttributeIds(
        selectedPersona,
        dimensions.data ?? [],
        measures.data ?? [],
        hierarchies.data ?? [],
      ),
    [selectedPersona, dimensions.data, measures.data, hierarchies.data],
  );

  const glossaryByTerm = useMemo(
    () => buildGlossaryIndex(glossary.data ?? []),
    [glossary.data],
  );

  const rows = useMemo<AttributeRow[]>(
    () =>
      buildAttributeRows({
        tables: tableList,
        attributesByTable,
        glossaryByTerm,
        visibleIds,
      }),
    [tableList, attributesByTable, glossaryByTerm, visibleIds],
  );

  const exportLabels = useMemo<ExportLabels>(
    () => ({
      headers: [
        t("modelDetails.colIndex"),
        t("modelDetails.colName"),
        t("modelDetails.colType"),
        t("modelDetails.colDisplay"),
        t("modelDetails.colDescription"),
        t("modelDetails.colKind"),
        t("modelDetails.colSource"),
        t("modelDetails.colFormula"),
      ],
      kindPhysical: t("modelDetails.kindPhysical"),
      kindUda: t("modelDetails.kindUda"),
    }),
    [t],
  );

  const slug = model.data?.slug ?? "";

  // The model's queryable columns (dimension + base-measure names). The binder
  // resolves SELECT references against these names only, so the source SELECT
  // must be built from them — not from raw physical attribute names, which
  // would fail to bind and force a fall back to the logical query.
  const selectableNames = useMemo(
    () =>
      selectableColumnNames({
        dimensions: dimensions.data ?? [],
        measures: measures.data ?? [],
        persona: selectedPersona,
      }),
    [dimensions.data, measures.data, selectedPersona],
  );

  // Logical model-level SELECT over every queryable column. Sent to the router
  // to be rewritten into the physical, joined, dialect-translated query — so
  // the result includes dimension-table columns reached via joins, not just
  // the fact-grain star that `SELECT *` degrades to.
  const modelSelectSql = useMemo(
    () => buildModelSelectSql(slug, selectableNames),
    [slug, selectableNames],
  );

  // Source SELECT — the query-router rewrite of the full-column model SELECT
  // for the selected persona, qualified and translated to the source dialect.
  const select = useQuery({
    queryKey: ["modelDetailsSelect", mid, personaId, modelSelectSql],
    queryFn: () =>
      queryRouterApiClient.explain(
        {
          model_id: mid,
          raw_query: modelSelectSql,
          dialect: "postgresql",
        },
        personaId,
      ),
    enabled: !!mid && !!modelSelectSql,
    retry: false,
  });

  // Client-side fallback (beautified model-level SELECT) used when the router
  // cannot rewrite the statement.
  const fallbackSelect = useMemo(
    () => beautifySql(modelSelectSql),
    [modelSelectSql],
  );

  const selectText = select.data?.rewritten_query
    ? beautifySql(select.data.rewritten_query)
    : fallbackSelect;

  function handleCopySelect() {
    if (!selectText) return;
    navigator.clipboard.writeText(selectText).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  const isLoading =
    model.isLoading ||
    sources.isLoading ||
    tables.isLoading;

  return (
    <Box>
      <Stack
        direction="row"
        spacing={1}
        alignItems="center"
        sx={{ mb: 2, flexWrap: "wrap", gap: 1 }}
      >
        <PersonaPicker
          projectId={pid}
          modelId={mid}
          value={personaId}
          onChange={setPersonaId}
          forAudience={false}
          label={t("modelDetails.persona")}
        />
        <Box sx={{ flex: 1 }} />
        <AttributeExportMenu
          rows={rows}
          labels={exportLabels}
          baseName={`${slug || "model"}-attributes`}
        />
      </Stack>

      {!isLoading && rows.length === 0 ? (
        <Alert severity="info">{t("modelDetails.noAttributes")}</Alert>
      ) : (
        <TableContainer sx={{ maxHeight: "45vh", mb: 2 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>{t("modelDetails.colIndex")}</TableCell>
                <TableCell>{t("modelDetails.colName")}</TableCell>
                <TableCell>{t("modelDetails.colType")}</TableCell>
                <TableCell>{t("modelDetails.colDisplay")}</TableCell>
                <TableCell>{t("modelDetails.colDescription")}</TableCell>
                <TableCell>{t("modelDetails.colKind")}</TableCell>
                <TableCell>{t("modelDetails.colSource")}</TableCell>
                <TableCell>{t("modelDetails.colFormula")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.id} hover>
                  <TableCell>{row.index}</TableCell>
                  <TableCell sx={{ fontFamily: "monospace" }}>
                    {row.name}
                  </TableCell>
                  <TableCell>{row.dataType}</TableCell>
                  <TableCell>{row.displayName}</TableCell>
                  <TableCell>{row.description || "—"}</TableCell>
                  <TableCell>
                    <Chip
                      size="small"
                      label={
                        row.kind === "user_defined"
                          ? t("modelDetails.kindUda")
                          : t("modelDetails.kindPhysical")
                      }
                      color={row.kind === "user_defined" ? "secondary" : "default"}
                      variant="outlined"
                    />
                  </TableCell>
                  <TableCell>{row.sourceTable}</TableCell>
                  <TableCell
                    sx={{ fontFamily: "monospace", whiteSpace: "pre-wrap" }}
                  >
                    {row.formula || "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <Box sx={{ display: "flex", alignItems: "center", mb: 0.5 }}>
        <Typography variant="subtitle2" sx={{ flex: 1 }}>
          {t("modelDetails.sourceSelect")}
        </Typography>
        <Tooltip
          title={copied ? t("modelDetails.copied") : t("modelDetails.copySelect")}
        >
          <span>
            <IconButton
              size="small"
              onClick={handleCopySelect}
              disabled={!selectText}
            >
              <ContentCopyIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Box>
      {select.isError && !fallbackSelect && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {t("modelDetails.selectFailed")}
        </Alert>
      )}
      <TextField
        value={selectText}
        multiline
        minRows={4}
        maxRows={14}
        fullWidth
        InputProps={{ readOnly: true }}
        sx={{ "& textarea": { fontFamily: "monospace", fontSize: 12 } }}
      />
    </Box>
  );
}
