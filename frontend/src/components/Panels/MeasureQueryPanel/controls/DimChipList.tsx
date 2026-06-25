import { useMemo, useState } from "react";
import { Box, Button, IconButton, ListSubheader, Menu, MenuItem, Stack, Tooltip, Typography } from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import CloseIcon from "@mui/icons-material/Close";
import KeyboardArrowLeftIcon from "@mui/icons-material/KeyboardArrowLeft";
import KeyboardArrowRightIcon from "@mui/icons-material/KeyboardArrowRight";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useT } from "../../../../i18n";
import type { Dimension } from "../../../../api/types";

type Props = {
  label: string;
  available: Dimension[];
  selectedIds: string[];
  max: number;
  disabledReasons?: Record<string, string>;
  onChange: (nextIds: string[]) => void;
};

function aliasGroupKey(d: Dimension): string {
  return d.source_table_alias ?? d.source_table_id ?? "_";
}

function aliasGroupLabel(d: Dimension): string {
  return d.source_table_display_name ?? d.source_table_alias ?? "";
}

export default function DimChipList({
  label,
  available,
  selectedIds,
  max,
  disabledReasons = {},
  onChange,
}: Props) {
  const t = useT();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const byId = new Map(available.map((d) => [d.id, d]));
  const selected = selectedIds.map((id) => byId.get(id)).filter((d): d is Dimension => !!d);
  const remaining = available.filter((d) => !selectedIds.includes(d.id));
  const canAdd = selectedIds.length < max && remaining.length > 0;

  const remainingGroups = useMemo(() => {
    const groups = new Map<string, { label: string; dims: Dimension[] }>();
    for (const d of remaining) {
      const key = aliasGroupKey(d);
      const entry = groups.get(key) ?? { label: aliasGroupLabel(d), dims: [] };
      entry.dims.push(d);
      groups.set(key, entry);
    }
    return Array.from(groups.values()).sort((a, b) =>
      a.label.localeCompare(b.label),
    );
  }, [remaining]);

  const showGroupHeaders = remainingGroups.length > 1;

  function moveSelected(from: number, to: number) {
    if (to < 0 || to >= selectedIds.length) return;
    const next = [...selectedIds];
    const [id] = next.splice(from, 1);
    next.splice(to, 0, id);
    onChange(next);
  }

  return (
    <Stack direction="row" spacing={0.75} sx={{ alignItems: "center", flexWrap: "wrap" }}>
      <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary", mr: 0.5 }}>
        {label}:
      </Typography>
      {selected.length === 0 && (
        <Typography variant="caption" color="text.secondary">
          {t("dimChip.none")}
        </Typography>
      )}
      {selected.map((d, idx) => (
        <Stack
          key={d.id}
          direction="row"
          alignItems="center"
          spacing={0.25}
          sx={{ px: 0.5, py: 0.1, border: "1px solid", borderColor: "divider", borderRadius: 1 }}
        >
          <Tooltip title={t("dimChip.moveLeft")}>
            <span>
              <IconButton
                size="small"
                disabled={idx === 0}
                onClick={() => moveSelected(idx, idx - 1)}
                sx={{ width: 18, height: 18 }}
              >
                <KeyboardArrowLeftIcon fontSize="inherit" />
              </IconButton>
            </span>
          </Tooltip>
          <Typography variant="caption" sx={{ whiteSpace: "nowrap" }}>
            {idx + 1}. {d.display_name}
          </Typography>
          {disabledReasons[d.id] && (
            <Tooltip title={disabledReasons[d.id]}>
              <WarningAmberIcon fontSize="inherit" sx={{ color: "warning.main", fontSize: 14 }} />
            </Tooltip>
          )}
          {d.high_cardinality && (
            <Tooltip title={t("dimChip.highCardinalityTooltipFull")}>
              <WarningAmberIcon fontSize="inherit" sx={{ color: "warning.main", fontSize: 14 }} />
            </Tooltip>
          )}
          <Tooltip title={t("dimChip.moveRight")}>
            <span>
              <IconButton
                size="small"
                disabled={idx === selected.length - 1}
                onClick={() => moveSelected(idx, idx + 1)}
                sx={{ width: 18, height: 18 }}
              >
                <KeyboardArrowRightIcon fontSize="inherit" />
              </IconButton>
            </span>
          </Tooltip>
          <Tooltip title={t("dimChip.remove")}>
            <IconButton
              size="small"
              onClick={() => onChange(selectedIds.filter((x) => x !== d.id))}
              sx={{ width: 18, height: 18 }}
            >
              <CloseIcon fontSize="inherit" />
            </IconButton>
          </Tooltip>
        </Stack>
      ))}
      <Box>
        <Button
          size="small"
          variant="outlined"
          startIcon={<AddIcon fontSize="small" />}
          disabled={!canAdd}
          onClick={(e) => setAnchor(e.currentTarget)}
          sx={{ py: 0.1, minWidth: 0 }}
        >
          {t("dimChip.add")}
        </Button>
        <Menu
          anchorEl={anchor}
          open={Boolean(anchor)}
          onClose={() => setAnchor(null)}
        >
          {remainingGroups.flatMap((g) => {
            const items = g.dims.map((d) => {
              const disabledReason = disabledReasons[d.id];
              const item = (
                <MenuItem
                key={d.id}
                disabled={Boolean(disabledReason)}
                onClick={() => {
                  onChange([...selectedIds, d.id]);
                  setAnchor(null);
                }}
                sx={{ pl: showGroupHeaders ? 3 : 2 }}
              >
                {d.display_name}
                {disabledReason && (
                  <WarningAmberIcon fontSize="small" sx={{ ml: 0.5, color: "warning.main", fontSize: 14 }} />
                )}
                {d.high_cardinality && (
                  <Tooltip title={t("dimChip.highCardinalityTooltip")}>
                    <WarningAmberIcon fontSize="small" sx={{ ml: 0.5, color: "warning.main", fontSize: 14 }} />
                  </Tooltip>
                )}
              </MenuItem>
              );
              return disabledReason ? (
                <Tooltip key={d.id} title={disabledReason} placement="right" arrow>
                  <span>{item}</span>
                </Tooltip>
              ) : item;
            });
            if (!showGroupHeaders) return items;
            return [
              <ListSubheader key={`${g.label}-h`} sx={{ lineHeight: "1.6em" }}>
                {g.label}
              </ListSubheader>,
              ...items,
            ];
          })}
        </Menu>
      </Box>
      {selectedIds.length >= max && (
        <Typography variant="caption" color="text.secondary">
          {t("dimChip.maxReached", { max: String(max) })}
        </Typography>
      )}
    </Stack>
  );
}
