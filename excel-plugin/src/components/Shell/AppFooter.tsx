import { Box, Typography } from '@mui/material';
import { tokens } from '../../theme';
import { strings } from '../../i18n/strings';
import type { Persona } from '../../types/tessallite';

interface AppFooterProps {
  personas: Persona[];
  activePersonaId: string | null;
  connected: boolean;
  /** True while the connection is lost and being retried. */
  reconnecting: boolean;
}

/** Connection and selected persona stay visible while the tab content scrolls. */
export default function AppFooter({ personas, activePersonaId, connected, reconnecting }: AppFooterProps) {
  const label = reconnecting ? strings.status.reconnecting : connected ? strings.status.connected : strings.status.disconnected;
  return (
    <Box
      component="footer"
      sx={{
        height: 22, flexShrink: 0, display: 'flex', alignItems: 'center', px: '10px',
        bgcolor: tokens.colorSubtleFill, borderTop: `1px solid ${tokens.colorBorderLight}`,
        gap: 1,
      }}
    >
      <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: reconnecting ? tokens.colorGoldDark : connected ? tokens.colorPrimary : tokens.colorTextSecondary }} />
      <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mr: 'auto' }}>{label}</Typography>
      <Typography noWrap sx={{ fontSize: 10, color: tokens.colorTextSecondary, minWidth: 0 }}>
        {strings.persona.label} {personas.find(p => p.id === activePersonaId)?.name || strings.persona.default}
      </Typography>
    </Box>
  );
}
