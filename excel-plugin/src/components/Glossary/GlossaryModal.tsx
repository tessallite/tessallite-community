import { useState, useMemo } from 'react';
import {
  Dialog, DialogTitle, DialogContent, TextField, Typography, Box, Chip,
  InputAdornment, ThemeProvider,
} from '@mui/material';
import { Search as SearchIcon } from '@mui/icons-material';
import { tokens, theme } from '../../theme';
import type { GlossaryEntry } from '../../types/tessallite';
import { strings } from '../../i18n/strings';

interface GlossaryModalProps {
  open: boolean;
  onClose: () => void;
  entries: GlossaryEntry[];
}

export default function GlossaryModal({ open, onClose, entries }: GlossaryModalProps) {
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    if (!search.trim()) return entries;
    const q = search.toLowerCase();
    return entries.filter(e =>
      e.term.toLowerCase().includes(q) ||
      e.definition.toLowerCase().includes(q) ||
      e.synonyms.some(s => s.toLowerCase().includes(q)),
    );
  }, [entries, search]);

  const sourceStyle: Record<string, { bg: string; text: string }> = {
    user: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
    llm: { bg: tokens.colorGoldBg, text: tokens.colorGoldDark },
    llm_approved: { bg: tokens.colorPrimaryBg, text: tokens.colorPrimary },
  };

  return (
    <ThemeProvider theme={theme}>
    <Dialog open={open} onClose={onClose} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 320, borderRadius: 2 } }}>
      <DialogTitle sx={{ fontSize: 14, fontWeight: 700, pb: 0 }}>{strings.glossary.title}</DialogTitle>
      <DialogContent sx={{ p: 1.5 }}>
        <TextField
          fullWidth
          size="small"
          placeholder={strings.glossary.searchPlaceholder}
          value={search}
          onChange={e => setSearch(e.target.value)}
          InputProps={{
            startAdornment: <InputAdornment position="start"><SearchIcon sx={{ fontSize: 18, color: tokens.colorTextSecondary }} /></InputAdornment>,
            sx: { fontSize: 13 },
          }}
          sx={{ mb: 1 }}
        />
        {filtered.length === 0 ? (
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, textAlign: 'center', py: 2 }}>
            {search ? strings.glossary.noMatch : strings.glossary.noEntries}
          </Typography>
        ) : (
          filtered.map(e => {
            const s = sourceStyle[e.source] || sourceStyle.llm;
            return (
              <Box key={e.id} sx={{ p: 1, mb: 0.5, border: `1px solid ${tokens.colorBorderLight}`, borderRadius: 1 }}>
                <Box sx={{ display: 'flex', gap: 0.5, mb: 0.25, flexWrap: 'wrap' }}>
                  <Typography sx={{ fontSize: 13, fontWeight: 600 }}>{e.term}</Typography>
                  <Chip label={e.source} size="small" sx={{ fontSize: 10, height: 20, bgcolor: s.bg, color: s.text }} />
                </Box>
                <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mb: 0.25 }}>{e.definition}</Typography>
                {e.synonyms.length > 0 && (
                  <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>
                    {strings.glossary.synonyms} {e.synonyms.join(', ')}
                  </Typography>
                )}
              </Box>
            );
          })
        )}
      </DialogContent>
    </Dialog>
    </ThemeProvider>
  );
}
