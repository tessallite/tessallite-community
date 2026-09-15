import { useRef, useState } from 'react';
import { Box, ButtonBase, Divider, IconButton, Menu, MenuItem, Popover, Select, Typography } from '@mui/material';
import { ExpandMore, FilterAltOutlined, MenuBookOutlined, Settings as SettingsIcon } from '@mui/icons-material';
import { tokens } from '../../theme';
import { strings } from '../../i18n/strings';
import ProfileSwitcher from '../ProfileSwitcher/ProfileSwitcher';
import PersonaDropdown from '../PersonaSwitcher/PersonaDropdown';
import type { ConnectionProfile } from '../../utils/storage';
import type { Persona } from '../../types/tessallite';
import { isTestProfileBuild, testProfileFailure } from '../../testProfile';

interface AppHeaderProps {
  profiles: ConnectionProfile[];
  activeProfile: ConnectionProfile | null;
  projects: { id: string; name: string }[];
  projectId: string | null;
  models: { id: string; name: string }[];
  modelId: string | null;
  personas: Persona[];
  activePersonaId: string | null;
  onProjectChange: (id: string) => void;
  onModelChange: (id: string) => void;
  onPersonaSelect: (persona: Persona | null) => void;
  onOpenDrill: () => void;
  onOpenGlossary: () => void;
  onOpenDiagnostics: () => void;
  onSwitchProfile: (profileId: string) => void;
  onRemoveProfile: (profileId: string) => void;
  onLogout: () => void;
}

function BrandMark({ large = false }: { large?: boolean }) {
  return <svg width={large ? 28 : 18} height={large ? 24 : 16} viewBox="0 0 32 28" fill="none" aria-hidden="true">
    <polygon points="0,7 8,0 16,7 8,14" fill={tokens.colorPrimaryDark} />
    <polygon points="16,7 24,0 32,7 24,14" fill={tokens.colorGold} />
    <polygon points="0,21 8,14 16,21 8,28" fill={tokens.colorPrimary} />
    <polygon points="16,21 24,14 32,21 24,28" fill={tokens.colorPrimaryDark} />
  </svg>;
}

/** Compact chrome; scope changes use App's existing transition callbacks. */
export default function AppHeader({
  profiles, activeProfile, projects, projectId, models, modelId, personas, activePersonaId,
  onProjectChange, onModelChange, onPersonaSelect, onOpenDrill, onOpenGlossary,
  onOpenDiagnostics, onSwitchProfile, onRemoveProfile, onLogout,
}: AppHeaderProps) {
  const [settingsAnchor, setSettingsAnchor] = useState<HTMLElement | null>(null);
  const [profileAnchor, setProfileAnchor] = useState<HTMLElement | null>(null);
  const [brandAnchor, setBrandAnchor] = useState<HTMLElement | null>(null);
  const [scopeAnchor, setScopeAnchor] = useState<HTMLElement | null>(null);
  const settingsButton = useRef<HTMLButtonElement>(null);
  const projectName = projects.find(p => p.id === projectId)?.name || strings.app.projectLabel;
  const modelName = models.find(m => m.id === modelId)?.name || strings.reportBuilder.modelLabel;
  const labelSx = { fontSize: 10, fontWeight: 600, textTransform: 'uppercase', color: tokens.colorTextSecondary, mb: 0.5 };
  const selectSx = { height: 26, fontSize: 12, borderRadius: '2px' };

  return <>
    <Box component="header" role="banner" sx={{ height: 36, flexShrink: 0, display: 'flex', alignItems: 'center', px: '10px', gap: '8px', borderBottom: '1px solid ' + tokens.colorBorderLight, bgcolor: tokens.colorWhite }}>
      <IconButton aria-label={strings.app.title} onClick={e => setBrandAnchor(e.currentTarget)} sx={{ p: 0, flexShrink: 0 }}><BrandMark /></IconButton>
      <ButtonBase aria-label={strings.app.scopeSelectorAria} aria-haspopup="dialog" aria-expanded={Boolean(scopeAnchor)} onClick={e => setScopeAnchor(e.currentTarget)} sx={{ flex: 1, minWidth: 0, justifyContent: 'flex-start', gap: 0.5 }}>
        <Typography noWrap sx={{ fontSize: 13, fontWeight: 600 }}>{projectName}</Typography>
        <Typography noWrap sx={{ fontSize: 12, color: tokens.colorTextSecondary }}>/ {modelName}</Typography>
        <ExpandMore sx={{ fontSize: 14, flexShrink: 0, color: tokens.colorTextSecondary }} />
      </ButtonBase>
      {isTestProfileBuild && <Typography data-testid="test-build-marker" sx={{ fontSize: 9, fontWeight: 700, color: tokens.colorWhite, bgcolor: tokens.colorCharcoal, px: 0.5 }}>
        {testProfileFailure() ? strings.app.testBuildSignInFailed : strings.app.testBuildMarker}
      </Typography>}
      <Box sx={{ display: 'flex', gap: '10px', '& .MuiIconButton-root': { p: 0 }, '& .MuiSvgIcon-root': { fontSize: 18, color: tokens.colorTextSecondary } }}>
        <IconButton title={strings.app.drillThrough} aria-label={strings.app.drillThroughCell} onClick={onOpenDrill}><FilterAltOutlined /></IconButton>
        <IconButton title={strings.app.glossary} aria-label={strings.app.glossaryAria} onClick={onOpenGlossary}><MenuBookOutlined /></IconButton>
        <IconButton ref={settingsButton} title={strings.app.settings} aria-label={strings.app.settingsAria} aria-haspopup="menu" onClick={e => setSettingsAnchor(e.currentTarget)}><SettingsIcon /></IconButton>
      </Box>
    </Box>
    <Popover anchorEl={brandAnchor} open={Boolean(brandAnchor)} onClose={() => setBrandAnchor(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 1.5 }}><BrandMark large /><Box>
        <Typography sx={{ fontSize: 13, fontWeight: 600 }}>{strings.app.title}</Typography>
        <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>{strings.app.subtitle}</Typography>
      </Box></Box>
    </Popover>
    <Popover anchorEl={scopeAnchor} open={Boolean(scopeAnchor)} onClose={() => setScopeAnchor(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}>
      <Box role="dialog" aria-label={strings.app.scopeSelectorAria} sx={{ width: 260, p: 1.5, display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Box><Typography id="scope-project-label" sx={labelSx}>{strings.app.projectLabel}</Typography>
          <Select fullWidth labelId="scope-project-label" inputProps={{ 'aria-label': strings.app.projectSelectorAria }} value={projectId || ''} disabled={!projects.length} onChange={e => onProjectChange(e.target.value)} sx={selectSx}>
            {projects.map(p => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
          </Select>
        </Box>
        <Box><Typography id="scope-model-label" sx={labelSx}>{strings.reportBuilder.modelLabel}</Typography>
          <Select fullWidth labelId="scope-model-label" value={modelId || ''} disabled={!models.length} onChange={e => onModelChange(e.target.value)} sx={selectSx}>
            {models.map(m => <MenuItem key={m.id} value={m.id}>{m.name}</MenuItem>)}
          </Select>
        </Box>
        <PersonaDropdown personas={personas} activePersonaId={activePersonaId} onSelect={onPersonaSelect} compact />
      </Box>
    </Popover>
    <Menu anchorEl={settingsAnchor} open={Boolean(settingsAnchor)} onClose={() => setSettingsAnchor(null)}>
      {activeProfile && <Box sx={{ px: 2, py: 0.5, maxWidth: 280 }}>
        <Typography noWrap sx={{ fontSize: 12 }}>{activeProfile.email}</Typography>
        <Typography noWrap sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>{strings.app.signedIn} · {activeProfile.name}</Typography>
      </Box>}
      {profiles.length > 0 && activeProfile && <MenuItem onClick={() => { setSettingsAnchor(null); setProfileAnchor(settingsButton.current); }}>{strings.profileSwitcher.switchProfileAria}…</MenuItem>}
      <MenuItem onClick={() => { setSettingsAnchor(null); onOpenDiagnostics(); }}>{strings.app.diagnosticsMenuItem}</MenuItem>
      <Divider />
      <MenuItem sx={{ color: tokens.colorRed }} onClick={() => { setSettingsAnchor(null); onLogout(); }}>{strings.app.signOut}</MenuItem>
    </Menu>
    <ProfileSwitcher profiles={profiles} activeProfile={activeProfile} onSwitch={onSwitchProfile} onRemove={onRemoveProfile} onLogout={onLogout} menuAnchor={profileAnchor} onMenuClose={() => setProfileAnchor(null)} />
  </>;
}
