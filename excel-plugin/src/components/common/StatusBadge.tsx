import { Chip, Box } from '@mui/material';
import { tokens } from '../../theme';

interface StatusBadgeProps {
  status: 'connected' | 'disconnected' | 'reconnecting' | 'active' | 'inactive';
  label: string;
  size?: 'small' | 'medium';
}

const colorMap: Record<StatusBadgeProps['status'], string> = {
  connected: tokens.colorPrimary,
  disconnected: tokens.colorTextSecondary,
  reconnecting: tokens.colorGold,
  active: tokens.colorPrimary,
  inactive: tokens.colorTextSecondary,
};

export default function StatusBadge({
  status,
  label,
  size = 'small',
}: StatusBadgeProps) {
  return (
    <Chip
      size={size}
      variant="outlined"
      label={label}
      avatar={
        <Box
          sx={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            bgcolor: colorMap[status],
          }}
        />
      }
      sx={{
        fontSize: 10,
        height: 22,
        borderColor: colorMap[status],
        color: colorMap[status],
        '& .MuiChip-avatar': { width: 8, height: 8, ml: 0.5 },
      }}
    />
  );
}
