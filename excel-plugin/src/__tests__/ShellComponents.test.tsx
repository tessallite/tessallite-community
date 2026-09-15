/**
 * Bug-9903: the task-pane shell (header, project bar, tab strip, offline
 * banner, footer) was inline JSX in App.tsx. It is now five components under
 * components/Shell. These tests pin the behaviour that moved, above all the
 * Bug-5965 keyboard contract of the tab strip: roving tabindex (only the
 * active tab is focusable), Arrow/Home/End move focus AND activate,
 * Enter/Space activate, and the aria wiring (tablist, tab, aria-selected,
 * aria-controls, aria-current) stays intact.
 */
import { describe, it, expect, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { AppHeader, ModeTabs, OfflineBanner, AppFooter, TAB_ORDER } from '../components/Shell';
import { strings } from '../i18n/strings';

const profile = { id: 'p1', name: 'Acme', serverUrl: 'https://t.example', tenantId: 'acme', email: 'j.doe@acme.example', createdAt: '2026-05-02T09:00:00Z' };
const scopeProps = { projects: [{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta' }], projectId: 'a', models: [{ id: 'm', name: 'Sales' }, { id: 'n', name: 'Costs' }], modelId: 'm', personas: [], activePersonaId: null, onProjectChange: vi.fn(), onModelChange: vi.fn(), onPersonaSelect: vi.fn() };

describe('ModeTabs (Bug-5965 keyboard contract)', () => {
  it('renders a tablist with one focusable, selected tab wired to its panel', () => {
    render(<ModeTabs mode="kpi" onModeChange={() => {}} askLabel="Ask Tessallite" />);
    const tabs = within(screen.getByRole('tablist')).getAllByRole('tab');
    expect(tabs.map(t => t.id)).toEqual(TAB_ORDER.map(m => `tab-${m}`));
    const active = screen.getByRole('tab', { selected: true });
    expect(active.id).toBe('tab-kpi');
    expect(active).toHaveAttribute('aria-controls', 'tabpanel-kpi');
    expect(active).toHaveAttribute('aria-current', 'page');
    expect(active).toHaveAttribute('tabindex', '0');
    tabs.filter(t => t !== active).forEach(t => {
      expect(t).toHaveAttribute('tabindex', '-1');
      expect(t).not.toHaveAttribute('aria-current');
    });
  });

  it('ArrowRight/ArrowLeft wrap, Home/End jump, and each move activates and focuses the target', () => {
    const onModeChange = vi.fn();
    render(<ModeTabs mode="report-builder" onModeChange={onModeChange} askLabel="Ask" />);
    const [first, second, third] = screen.getAllByRole('tab');
    fireEvent.keyDown(first, { key: 'ArrowRight' });
    expect(onModeChange).toHaveBeenLastCalledWith('kpi');
    expect(document.activeElement).toBe(second);
    fireEvent.keyDown(first, { key: 'ArrowLeft' });
    expect(onModeChange).toHaveBeenLastCalledWith('ask');
    expect(document.activeElement).toBe(third);
    fireEvent.keyDown(third, { key: 'Home' });
    expect(onModeChange).toHaveBeenLastCalledWith('report-builder');
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: 'End' });
    expect(onModeChange).toHaveBeenLastCalledWith('ask');
    expect(document.activeElement).toBe(third);
  });

  it('Enter and Space activate the focused tab; other keys do nothing', () => {
    const onModeChange = vi.fn();
    render(<ModeTabs mode="report-builder" onModeChange={onModeChange} askLabel="Ask" />);
    const [, second] = screen.getAllByRole('tab');
    fireEvent.keyDown(second, { key: 'Enter' });
    expect(onModeChange).toHaveBeenLastCalledWith('kpi');
    fireEvent.keyDown(second, { key: ' ' });
    expect(onModeChange).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(second, { key: 'a' });
    expect(onModeChange).toHaveBeenCalledTimes(2);
    fireEvent.click(second);
    expect(onModeChange).toHaveBeenCalledTimes(3);
  });

  it('shows the agent display name on the Ask tab', () => {
    render(<ModeTabs mode="ask" onModeChange={() => {}} askLabel="Ask Atlas" />);
    expect(screen.getByRole('tab', { selected: true })).toHaveTextContent('Ask Atlas');
  });
});

describe('AppHeader', () => {
  it('renders the brand, fires the drill and glossary actions, and routes the settings menu', () => {
    const onOpenDrill = vi.fn(), onOpenGlossary = vi.fn(), onOpenDiagnostics = vi.fn(), onLogout = vi.fn(), onSwitchProfile = vi.fn();
    render(<AppHeader {...scopeProps} profiles={[profile, { ...profile, id: 'p2', name: 'Other profile' }]} activeProfile={profile} onOpenDrill={onOpenDrill} onOpenGlossary={onOpenGlossary}
      onOpenDiagnostics={onOpenDiagnostics} onSwitchProfile={onSwitchProfile} onRemoveProfile={() => {}} onLogout={onLogout} />);
    expect(screen.getByRole('button', { name: strings.app.title })).toBeInTheDocument();
    expect(screen.getByRole('banner')).toHaveTextContent('Alpha');
    fireEvent.click(screen.getByRole('button', { name: strings.app.drillThroughCell }));
    fireEvent.click(screen.getByRole('button', { name: strings.app.glossaryAria }));
    expect(onOpenDrill).toHaveBeenCalledTimes(1);
    expect(onOpenGlossary).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: strings.app.settingsAria }));
    fireEvent.click(screen.getByText(strings.app.diagnosticsMenuItem));
    expect(onOpenDiagnostics).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: strings.app.settingsAria }));
    fireEvent.click(screen.getByText(`${strings.profileSwitcher.switchProfileAria}…`));
    fireEvent.click(screen.getByText('Other profile'));
    expect(onSwitchProfile).toHaveBeenLastCalledWith('p2');
    fireEvent.click(screen.getByRole('button', { name: strings.app.settingsAria }));
    fireEvent.click(screen.getByRole('menuitem', { name: strings.app.signOut }));
    expect(onLogout).toHaveBeenCalledTimes(1);
  });
});

describe('Scope popover', () => {
  it('keeps project, model and default persona changes reachable in the header', () => {
    render(<AppHeader {...scopeProps} profiles={[profile]} activeProfile={profile} onOpenDrill={() => {}} onOpenGlossary={() => {}}
      onOpenDiagnostics={() => {}} onSwitchProfile={() => {}} onRemoveProfile={() => {}} onLogout={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: strings.app.scopeSelectorAria }));
    const dialog = screen.getByRole('dialog', { name: strings.app.scopeSelectorAria });
    fireEvent.mouseDown(within(dialog).getAllByRole('combobox')[0]);
    fireEvent.click(screen.getByRole('option', { name: 'Beta' }));
    expect(scopeProps.onProjectChange).toHaveBeenLastCalledWith('b');
    fireEvent.mouseDown(within(dialog).getAllByRole('combobox')[1]);
    fireEvent.click(screen.getByRole('option', { name: 'Costs' }));
    expect(scopeProps.onModelChange).toHaveBeenLastCalledWith('n');
    fireEvent.mouseDown(within(dialog).getAllByRole('combobox')[2]);
    fireEvent.click(screen.getByRole('option', { name: strings.persona.default }));
    expect(within(dialog).getAllByRole('combobox')).toHaveLength(3);
  });
});

describe('OfflineBanner', () => {
  it('announces the lost connection and retries', () => {
    const onRetry = vi.fn();
    render(<OfflineBanner onRetry={onRetry} />);
    expect(screen.getByRole('alert')).toHaveTextContent(strings.connection.lost);
    fireEvent.click(screen.getByRole('button', { name: strings.app.retry }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe('AppFooter', () => {
  it('shows the persona and the three connection states', () => {
    const { rerender } = render(<AppFooter personas={[]} activePersonaId={null} connected reconnecting={false} />);
    expect(screen.getByRole('contentinfo')).toHaveTextContent(strings.persona.default);
    expect(screen.getByText(strings.status.connected)).toBeInTheDocument();
    rerender(<AppFooter personas={[]} activePersonaId={null} connected={false} reconnecting />);
    expect(screen.getByText(strings.status.reconnecting)).toBeInTheDocument();
    rerender(<AppFooter personas={[]} activePersonaId={null} connected={false} reconnecting={false} />);
    expect(screen.getByText(strings.status.disconnected)).toBeInTheDocument();
  });
});
