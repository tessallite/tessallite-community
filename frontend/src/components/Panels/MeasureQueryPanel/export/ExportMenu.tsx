import { useState } from "react";
import { Button, Menu, MenuItem } from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import { useT } from "../../../../i18n";
import type { ExecuteResponse, Measure } from "../../../../api/types";
import type { PivotModel } from "../types";
import type { TotalsModel } from "../totals";
import type { EmptyCellMode } from "../grid/PivotGrid";
import { pivotToCsv } from "./csv";
import { pivotToXlsx } from "./xlsx";
import { downloadText, safeFilename } from "./download";

type Props = {
  pivot: PivotModel;
  measure: Measure;
  // F-019-08: every column measure beyond the first, so the export carries
  // all measures the grid shows (not just the first column).
  extraMeasures?: Measure[];
  executeResult: ExecuteResponse;
  // Totals for the first measure (legacy single-measure XLSX path).
  totals?: TotalsModel | null;
  // F-019-08: per-measure totals (keyed by measure.name) so subtotals/grand
  // totals export for every measure.
  allTotals?: Map<string, TotalsModel | null> | null;
  showSubtotals?: boolean;
  showGrandTotals?: boolean;
  emptyCellMode?: EmptyCellMode;
  // F-019-08: the grid's current sorted row order so the export honours sort.
  rowKeyOrder?: string[][];
};

export default function ExportMenu({
  pivot,
  measure,
  extraMeasures,
  executeResult,
  totals,
  allTotals,
  showSubtotals,
  showGrandTotals,
  emptyCellMode,
  rowKeyOrder,
}: Props) {
  const t = useT();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const base = safeFilename(measure.display_name);

  function close() {
    setAnchor(null);
  }

  function handleCsv() {
    downloadText(
      `${base}.csv`,
      "text/csv",
      pivotToCsv(pivot, measure, {
        extraMeasures,
        allTotals,
        showSubtotals,
        showGrandTotals,
        emptyCellMode,
        rowKeyOrder,
      }),
    );
    close();
  }

  function handleJson() {
    downloadText(
      `${base}.json`,
      "application/json",
      JSON.stringify(executeResult, null, 2),
    );
    close();
  }

  async function handleXlsx() {
    close();
    const blob = await pivotToXlsx(pivot, measure, {
      extraMeasures,
      totals,
      allTotals,
      showSubtotals,
      showGrandTotals,
      emptyCellMode,
      rowKeyOrder,
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${base}.xlsx`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <Button
        size="small"
        variant="outlined"
        startIcon={<DownloadIcon fontSize="small" />}
        onClick={(e) => setAnchor(e.currentTarget)}
      >
        {t("exportMenu.export")}
      </Button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={close}>
        <MenuItem onClick={handleXlsx}>{t("exportMenu.excel")}</MenuItem>
        <MenuItem onClick={handleCsv}>{t("exportMenu.csv")}</MenuItem>
        <MenuItem onClick={handleJson}>{t("exportMenu.json")}</MenuItem>
      </Menu>
    </>
  );
}
