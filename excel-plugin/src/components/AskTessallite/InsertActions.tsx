import { Box, Button, IconButton, Tooltip, Typography } from '@mui/material';
import type { ReactNode } from 'react';
import {
  AccountTreeOutlined,
  BarChartOutlined,
  FunctionsOutlined,
  ManageSearchOutlined,
  OpenInNew,
  PivotTableChartOutlined,
  TableChartOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import { strings, templates } from '../../i18n/strings';

interface InsertActionsProps {
  data: Record<string, unknown>[];
  headers: string[];
  onInsertTable?: () => void;
  // Phase 3: Chart, Local Pivot, CUBE formulas, Live connection, Show Query
  onInsertChart?: () => void;
  onLocalPivot?: () => void;
  onCubeFormulas?: () => void;
  onLiveConnection?: () => void;
  onShowQuery?: () => void;
  onPopout?: () => void;
  popoutTitle?: string;
  compact?: boolean;
  // Phase 3: recommendedAction highlighting
  recommendedAction?: 'table' | 'chart' | 'pivot' | 'cube';
}

export default function InsertActions({
  data,
  headers,
  onInsertTable,
  onInsertChart,
  onLocalPivot,
  onCubeFormulas,
  onLiveConnection,
  onShowQuery,
  onPopout,
  popoutTitle,
  compact = false,
  recommendedAction,
}: InsertActionsProps) {
  const rowCount = data.length;
  const colCount = headers.length;

  const recommendedStyle = (action: string) =>
    action === recommendedAction
      ? { borderColor: tokens.colorGoldDark, borderWidth: 2, bgcolor: tokens.colorGoldDark, color: '#fff', '&:hover': { bgcolor: tokens.colorGoldDark, opacity: 0.9 } }
      : {};

  if (compact) {
    const actionButton = (
      label: string,
      icon: ReactNode,
      onClick: (() => void) | undefined,
      action?: string,
    ) => (
      <Tooltip key={label} title={label}>
        <span>
          <IconButton
            size="small"
            aria-label={label}
            disabled={!onClick}
            onClick={onClick}
            sx={{
              width: 26,
              height: 24,
              p: 0,
              borderRadius: 0.5,
              bgcolor: onClick
                ? action === recommendedAction
                  ? tokens.colorGoldDark
                  : tokens.colorPrimary
                : "transparent",
              color: onClick ? "#fff" : "#c4c4c4",
              border: onClick ? 0 : `1px solid ${tokens.colorBorderLight}`,
              "&:hover": onClick
                ? {
                    bgcolor:
                      action === recommendedAction
                        ? tokens.colorGoldDark
                        : tokens.colorPrimaryDark,
                  }
                : undefined,
            }}
          >
            {icon}
          </IconButton>
        </span>
      </Tooltip>
    );

    return (
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          gap: 0.5,
          minWidth: 0,
          flexShrink: 1,
          flexWrap: "wrap",
        }}
      >
        {actionButton(strings.insertActions.chart, <BarChartOutlined sx={{ fontSize: 15 }} />, onInsertChart, "chart")}
        {actionButton(strings.insertActions.insertTable, <TableChartOutlined sx={{ fontSize: 15 }} />, onInsertTable, "table")}
        {actionButton(strings.insertActions.localPivot, <PivotTableChartOutlined sx={{ fontSize: 15 }} />, onLocalPivot, "pivot")}
        {onPopout && (
          <IconButton
            size="small"
            aria-label={popoutTitle || strings.chartPopout.tooltip}
            title={popoutTitle || strings.chartPopout.tooltip}
            onClick={onPopout}
            sx={{ width: 26, height: 24, p: 0, border: 1, borderColor: tokens.colorBorder, borderRadius: 0.5 }}
          >
            <OpenInNew sx={{ fontSize: 14 }} />
          </IconButton>
        )}
        {(onCubeFormulas || onLiveConnection || onShowQuery) && (
          <Box sx={{ width: "1px", height: 16, bgcolor: tokens.colorBorderLight, mx: 0.25 }} />
        )}
        {onCubeFormulas && actionButton(strings.insertActions.cubeFormulas, <FunctionsOutlined sx={{ fontSize: 15 }} />, onCubeFormulas)}
        {onLiveConnection && actionButton(strings.insertActions.liveConnection, <AccountTreeOutlined sx={{ fontSize: 15 }} />, onLiveConnection)}
        {onShowQuery && actionButton(strings.insertActions.showQuery, <ManageSearchOutlined sx={{ fontSize: 15 }} />, onShowQuery)}
      </Box>
    );
  }

  return (
    <Box sx={{ mt: 0.5 }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
        {onInsertTable && (
          <Button
            size="small" variant="contained"
            sx={{ fontSize: 11, textTransform: 'none', ...recommendedStyle('table') }}
            onClick={onInsertTable}
          >
            {strings.insertActions.insertTable}
          </Button>
        )}
        {onInsertChart && (
          <Button
            size="small" variant="contained"
            sx={{ fontSize: 11, textTransform: 'none', ...recommendedStyle('chart') }}
            onClick={onInsertChart}
          >
            {strings.insertActions.chart}
          </Button>
        )}
        {onLocalPivot && (
          <Button
            size="small" variant="contained"
            sx={{ fontSize: 11, textTransform: 'none', ...recommendedStyle('pivot') }}
            onClick={onLocalPivot}
          >
            {strings.insertActions.localPivot}
          </Button>
        )}
        {onCubeFormulas && (
          <Button size="small" variant="outlined" sx={{ fontSize: 11, textTransform: 'none' }} onClick={onCubeFormulas}>
            {strings.insertActions.cubeFormulas}
          </Button>
        )}
        {onLiveConnection && (
          <Button size="small" variant="outlined" sx={{ fontSize: 11, textTransform: 'none' }} onClick={onLiveConnection}>
            {strings.insertActions.liveConnection}
          </Button>
        )}
        {onShowQuery && (
          <Button size="small" variant="outlined" sx={{ fontSize: 11, textTransform: 'none', color: tokens.colorTextSecondary }} onClick={onShowQuery}>
            {strings.insertActions.showQuery}
          </Button>
        )}
      </Box>
      {rowCount > 0 && (
        <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mt: 0.5 }}>
          {templates.insertActions.dimensions(rowCount, colCount)}
        </Typography>
      )}
    </Box>
  );
}
