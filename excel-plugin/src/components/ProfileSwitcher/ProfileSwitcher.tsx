import { useState } from 'react';
import {
  IconButton, Menu, MenuItem, ListItemText, Typography, Dialog,
  DialogTitle, DialogContent, DialogActions, Button, Divider, Box,
} from '@mui/material';
import { PersonOutline } from '@mui/icons-material';
import { tokens } from '../../theme';
import type { ConnectionProfile } from '../../utils/storage';

interface ProfileSwitcherProps {
  profiles: ConnectionProfile[];
  activeProfile: ConnectionProfile | null;
  onSwitch: (profileId: string) => void;
  onRemove: (profileId: string) => void;
  onLogout: () => void;
}

export default function ProfileSwitcher({
  profiles,
  activeProfile,
  onSwitch,
  onRemove,
  onLogout,
}: ProfileSwitcherProps) {
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
  const [removeTarget, setRemoveTarget] = useState<string | null>(null);

  const activeName = activeProfile
    ? (activeProfile.name.length > 18 ? activeProfile.name.slice(0, 17) + '\u2026' : activeProfile.name)
    : '';

  return (
    <>
      <IconButton
        size="small"
        title="Connection Profiles"
        aria-label="Switch profile"
        onClick={e => setAnchorEl(e.currentTarget)}
      >
        <PersonOutline sx={{ fontSize: 20, color: tokens.colorTextSecondary }} />
      </IconButton>

      <Menu
        anchorEl={anchorEl}
        open={Boolean(anchorEl)}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        <Box sx={{ px: 2, py: 0.5 }}>
          <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, textTransform: 'uppercase' }}>
            Connection Profiles
          </Typography>
        </Box>

        {profiles.length === 0 && (
          <MenuItem disabled>
            <ListItemText
              primary="No saved connections"
              primaryTypographyProps={{ fontSize: 12 }}
            />
          </MenuItem>
        )}

        {profiles.map(p => {
          const isActive = activeProfile?.id === p.id;
          return (
            <MenuItem
              key={p.id}
              onClick={() => { onSwitch(p.id); setAnchorEl(null); }}
              selected={isActive}
              sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}
            >
              <ListItemText
                primary={isActive ? `${p.name} \u2713` : p.name}
                secondary={`${p.email} / ${p.tenantId}`}
                primaryTypographyProps={{ fontSize: 13 }}
                secondaryTypographyProps={{ fontSize: 10 }}
              />
              {!isActive && (
                <IconButton
                  size="small"
                  onClick={e => {
                    e.stopPropagation();
                    setRemoveTarget(p.id);
                  }}
                  sx={{ ml: 0.5, fontSize: 14, color: tokens.colorTextSecondary }}
                >
                  &times;
                </IconButton>
              )}
            </MenuItem>
          );
        })}

        <Divider />
        <MenuItem
          onClick={() => { setAnchorEl(null); onLogout(); }}
          sx={{ color: tokens.colorRed }}
        >
          <ListItemText
            primary="Sign Out"
            primaryTypographyProps={{ fontSize: 13 }}
          />
        </MenuItem>
      </Menu>

      <Dialog open={Boolean(removeTarget)} onClose={() => setRemoveTarget(null)}>
        <DialogTitle sx={{ fontSize: 14, fontWeight: 700 }}>
          Remove Profile
        </DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: 13 }}>
            This will remove the saved connection. You can log in again to restore it. Continue?
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button size="small" onClick={() => setRemoveTarget(null)} sx={{ textTransform: 'none' }}>
            Cancel
          </Button>
          <Button
            size="small"
            variant="contained"
            onClick={() => {
              if (removeTarget) onRemove(removeTarget);
              setRemoveTarget(null);
              setAnchorEl(null);
            }}
            sx={{ textTransform: 'none' }}
          >
            Remove
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
