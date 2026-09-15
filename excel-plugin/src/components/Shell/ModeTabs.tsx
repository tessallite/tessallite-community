import { useCallback, useRef } from 'react';
import { Box, Typography } from '@mui/material';
import { AnalyticsOutlined, AutoAwesomeOutlined, SpeedOutlined } from '@mui/icons-material';
import { tokens } from '../../theme';
import { strings } from '../../i18n/strings';

export type AppMode = 'ask' | 'report-builder' | 'kpi';

/** Rendered order of the task-pane tabs; drives keyboard navigation. */
export const TAB_ORDER: readonly AppMode[] = ['report-builder', 'kpi', 'ask'];

interface ModeTabsProps {
  mode: AppMode;
  onModeChange: (mode: AppMode) => void;
  /** Label of the Ask tab: the agent's display name when configured. */
  askLabel: string;
}

/**
 * The three-section tab strip under the task-pane header.
 *
 * Bug-5965: roving tabindex. Only the active tab is in the tab order; Arrow
 * keys, Home and End move focus AND activate; Enter/Space activate the
 * focused tab. `aria-controls` targets `tabpanel-<mode>` rendered by App.
 */
export default function ModeTabs({ mode, onModeChange, askLabel }: ModeTabsProps) {
  const tabRefs = useRef<(HTMLDivElement | null)[]>([]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent, index: number) => {
    const count = TAB_ORDER.length;
    let next: number | null = null;
    switch (e.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        next = (index + 1) % count;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        next = (index - 1 + count) % count;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = count - 1;
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        onModeChange(TAB_ORDER[index]);
        return;
      default:
        return;
    }
    e.preventDefault();
    onModeChange(TAB_ORDER[next]);
    tabRefs.current[next]?.focus();
  }, [onModeChange]);

  const tabs = [
    { id: 'report-builder' as const, label: strings.app.tabAnalyse, icon: AnalyticsOutlined },
    { id: 'kpi' as const, label: strings.app.tabKpis, icon: SpeedOutlined },
    { id: 'ask' as const, label: askLabel, icon: AutoAwesomeOutlined },
  ];

  return (
    <Box
      component="nav"
      role="tablist"
      aria-label={strings.app.sectionsAria}
      sx={{
        height: 32, flexShrink: 0, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr',
        borderBottom: `1px solid ${tokens.colorBorderLight}`,
        bgcolor: tokens.colorSubtleFill,
      }}
    >
      {tabs.map((tab, index) => {
        const active = mode === tab.id;
        const Icon = tab.icon;
        return (
          <Box
            key={tab.id}
            ref={(el: HTMLDivElement | null) => { tabRefs.current[index] = el; }}
            role="tab"
            id={`tab-${tab.id}`}
            aria-controls={`tabpanel-${tab.id}`}
            aria-selected={active}
            aria-current={active ? 'page' : undefined}
            tabIndex={active ? 0 : -1}
            onKeyDown={(e) => handleKeyDown(e, index)}
            onClick={() => onModeChange(tab.id)}
            sx={{
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              gap: 0.5,
              minWidth: 0,
              px: 0.5,
              cursor: 'pointer',
              fontSize: 12, fontWeight: 600,
              color: active ? tokens.colorPrimary : tokens.colorTextSecondary,
              bgcolor: active ? tokens.colorWhite : 'transparent',
              borderBottom: active ? `2px solid ${tokens.colorPrimary}` : '2px solid transparent',
              '&:hover': {
                bgcolor: tokens.colorWhite,
                color: active ? tokens.colorPrimary : tokens.colorCharcoal,
              },
              '&:focus-visible': {
                outline: `2px solid ${tokens.colorPrimary}`,
                outlineOffset: '-2px',
              },
            }}
          >
            <Icon sx={{ fontSize: 17, flexShrink: 0 }} />
            <Typography sx={{ fontSize: 12, fontWeight: 600, color: 'inherit', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {tab.label}
            </Typography>
          </Box>
        );
      })}
    </Box>
  );
}
