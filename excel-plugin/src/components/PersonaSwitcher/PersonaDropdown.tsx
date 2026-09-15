import { Box, Typography, Select, MenuItem, Chip } from '@mui/material';
import { tokens } from '../../theme';
import type { Persona } from '../../types/tessallite';
import { strings } from '../../i18n/strings';

interface PersonaDropdownProps {
  personas: Persona[];
  activePersonaId: string | null;
  onSelect: (persona: Persona | null) => void;
  compact?: boolean;
}

const audienceStyle: Record<string, { bg: string; text: string }> = {
  business: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
  technical: { bg: tokens.colorPurpleBg, text: tokens.colorPurple },
};

export default function PersonaDropdown({ personas, activePersonaId, onSelect, compact = false }: PersonaDropdownProps) {
  if (personas.length === 0 && !compact) return null;

  return (
    <Box sx={{ display: 'flex', flexDirection: compact ? 'column' : 'row', alignItems: compact ? 'stretch' : 'center', gap: 0.5 }}>
      <Typography sx={{ fontSize: compact ? 10 : 11, fontWeight: compact ? 600 : 400, textTransform: compact ? 'uppercase' : 'none', color: tokens.colorTextSecondary }}>
        {strings.persona.label}
      </Typography>
      <Select
        size="small"
        inputProps={{ 'aria-label': strings.persona.label }}
        value={activePersonaId || ''}
        onChange={e => {
          const pid = e.target.value as string;
          onSelect(pid ? personas.find(p => p.id === pid) || null : null);
        }}
        sx={{
          fontSize: compact ? 12 : 11, height: compact ? 26 : 22, borderRadius: '2px',
          '& .MuiSelect-select': { py: 0, pr: 3 },
          ...(!compact && { '& fieldset': { border: 'none' } }),
        }}
        displayEmpty
      >
        <MenuItem value="">
          <Typography sx={{ fontSize: 11 }}>{strings.persona.default}</Typography>
        </MenuItem>
        {personas.map(p => {
          const aud = audienceStyle[p.audience || ''] || audienceStyle.business;
          return (
            <MenuItem key={p.id} value={p.id}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, width: '100%' }}>
                <Typography sx={{ fontSize: 11, flex: 1 }}>{p.name}</Typography>
                {p.audience && (
                  <Chip label={p.audience} size="small" sx={{ fontSize: 9, height: 16, bgcolor: aud.bg, color: aud.text }} />
                )}
              </Box>
            </MenuItem>
          );
        })}
      </Select>
    </Box>
  );
}
