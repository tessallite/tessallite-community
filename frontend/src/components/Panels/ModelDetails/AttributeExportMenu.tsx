import { useState } from "react";
import { Button, Menu, MenuItem } from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import { useT } from "../../../i18n";
import { downloadText, safeFilename } from "../MeasureQueryPanel/export/download";
import {
  rowsToCsv,
  rowsToJson,
  rowsToText,
  rowsToXlsx,
  type AttributeRow,
  type ExportLabels,
} from "./attributeRows";

interface Props {
  rows: AttributeRow[];
  labels: ExportLabels;
  baseName: string;
  disabled?: boolean;
}

export default function AttributeExportMenu({
  rows,
  labels,
  baseName,
  disabled,
}: Props) {
  const t = useT();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const base = safeFilename(baseName);

  function close() {
    setAnchor(null);
  }

  function handleCsv() {
    downloadText(`${base}.csv`, "text/csv", rowsToCsv(rows, labels));
    close();
  }

  function handleJson() {
    downloadText(`${base}.json`, "application/json", rowsToJson(rows, labels));
    close();
  }

  function handleText() {
    downloadText(`${base}.txt`, "text/plain", rowsToText(rows, labels));
    close();
  }

  async function handleXlsx() {
    close();
    const blob = await rowsToXlsx(rows, labels);
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
        disabled={disabled || rows.length === 0}
      >
        {t("exportMenu.export")}
      </Button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={close}>
        <MenuItem onClick={handleXlsx}>{t("exportMenu.excel")}</MenuItem>
        <MenuItem onClick={handleCsv}>{t("exportMenu.csv")}</MenuItem>
        <MenuItem onClick={handleJson}>{t("exportMenu.json")}</MenuItem>
        <MenuItem onClick={handleText}>{t("modelDetails.exportText")}</MenuItem>
      </Menu>
    </>
  );
}
