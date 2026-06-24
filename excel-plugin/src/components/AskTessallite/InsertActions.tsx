import { Box, Button, Typography } from '@mui/material';
import { tokens } from '../../theme';

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
            Insert Table
          </Button>
        )}
        {onInsertChart && (
          <Button
            size="small" variant="contained"
            sx={{ fontSize: 11, textTransform: 'none', ...recommendedStyle('chart') }}
            onClick={onInsertChart}
          >
            Chart
          </Button>
        )}
        {onLocalPivot && (
          <Button
            size="small" variant="contained"
            sx={{ fontSize: 11, textTransform: 'none', ...recommendedStyle('pivot') }}
            onClick={onLocalPivot}
          >
            Local Pivot
          </Button>
        )}
        {onCubeFormulas && (
          <Button size="small" variant="outlined" sx={{ fontSize: 11, textTransform: 'none' }} onClick={onCubeFormulas}>
            CUBE formulas
          </Button>
        )}
        {onLiveConnection && (
          <Button size="small" variant="outlined" sx={{ fontSize: 11, textTransform: 'none' }} onClick={onLiveConnection}>
            Live connection
          </Button>
        )}
        {onShowQuery && (
          <Button size="small" variant="outlined" sx={{ fontSize: 11, textTransform: 'none', color: tokens.colorTextSecondary }} onClick={onShowQuery}>
            Show Query
          </Button>
        )}
      </Box>
      {rowCount > 0 && (
        <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mt: 0.5 }}>
          {rowCount} rows x {colCount} columns
        </Typography>
      )}
    </Box>
  );
}
