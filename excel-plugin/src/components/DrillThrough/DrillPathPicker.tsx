import { Box, Typography } from '@mui/material';
import { tokens } from '../../theme';
import type { DrillOption } from '../../types/tessallite';
import { strings } from '../../i18n/strings';

interface DrillPathPickerProps {
  options: DrillOption[];
  selectedPath: string;
  onSelect: (hierarchyId: string) => void;
}

export default function DrillPathPicker({
  options,
  selectedPath,
  onSelect,
}: DrillPathPickerProps) {
  if (options.length === 0) return null;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25, px: 1.5, py: 0.5 }}>
      <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary, mb: 0.25 }}>
        {strings.drill.drillPaths}
      </Typography>
      {options.map(o => (
        <Box
          key={o.hierarchy_id}
          onClick={() => onSelect(o.hierarchy_id)}
          sx={{
            p: 0.5,
            px: 1,
            borderRadius: 1,
            cursor: 'pointer',
            fontSize: 11,
            bgcolor: o.hierarchy_id === selectedPath ? tokens.colorPrimaryBg : 'transparent',
            border: o.hierarchy_id === selectedPath
              ? `1px solid ${tokens.colorPrimary}`
              : '1px solid transparent',
            '&:hover': { bgcolor: tokens.colorSubtleFill },
          }}
        >
          <Typography sx={{ fontSize: 11, fontWeight: o.hierarchy_id === selectedPath ? 600 : 400 }}>
            {o.hierarchy_name}
          </Typography>
          {o.current_level_name && o.next_level_name && (
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>
              {o.current_level_name} {'>'} {o.next_level_name}
            </Typography>
          )}
        </Box>
      ))}
    </Box>
  );
}
