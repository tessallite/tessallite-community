import { Box, Button, Typography } from '@mui/material';
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
  recommendedAction,
}: InsertActionsProps) {
  const rowCount = data.length;
  const colCount = headers.length;

  const recommendedStyle = (action: string) =>
    action === recommendedAction
      ? { borderColor: tokens.colorGoldDark, borderWidth: 2, bgcolor: tokens.colorGoldDark, color: '#fff', '&:hover': { bgcolor: tokens.colorGoldDark, opacity: 0.9 } }
      : {};

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
