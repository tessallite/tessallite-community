/**
 * Bug-7397 R12-5 — the refresh skip/warning reasons must be READABLE.
 *
 * `refreshTables` computes a per-table reason for every skip and warning, and
 * `templates.tableRefresh.skippedDetail` existed to render them -- with ZERO
 * call sites anywhere in src/. The UI showed only "N skipped (see details)"
 * with no details behind it, so every honest reason the R6-R11 redesign wrote
 * (concurrent modification, table resized, blocked blocks, corrupted
 * provenance) was unreachable text: the user could not tell which table failed,
 * why, or that clicking Refresh again would clear a transient skip.
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import RefreshDetailsPanel from '../components/ReportBuilder/RefreshDetailsPanel';
import { buildRefreshDetailRows, type RefreshResult } from '../utils/tableRefresh';
import { strings, templates } from '../i18n/strings';

describe('buildRefreshDetailRows', () => {
  it('flattens skipped and warned tables into one displayable list, tagged by kind', () => {
    const result: RefreshResult = {
      refreshed: ['Table3'],
      skipped: [{ name: 'Table1', reason: strings.tableRefresh.tableResizedSkip }],
      warnings: [{ name: 'Table3', reason: strings.tableRefresh.provenanceWriteBackFailed }],
    };
    expect(buildRefreshDetailRows(result)).toEqual([
      { name: 'Table1', reason: strings.tableRefresh.tableResizedSkip, kind: 'skipped' },
      { name: 'Table3', reason: strings.tableRefresh.provenanceWriteBackFailed, kind: 'warning' },
    ]);
  });

  it('produces nothing when every table refreshed cleanly (no empty details affordance)', () => {
    expect(buildRefreshDetailRows({ refreshed: ['A'], skipped: [], warnings: [] })).toEqual([]);
  });
});

describe('RefreshDetailsPanel', () => {
  const ROWS = buildRefreshDetailRows({
    refreshed: ['Sales'],
    skipped: [
      { name: 'Orders', reason: strings.tableRefresh.concurrentModificationSkip },
      { name: 'Margins', reason: strings.tableRefresh.lockBusySkip },
    ],
    warnings: [{ name: 'Sales', reason: strings.tableRefresh.provenanceWriteBackFailed }],
  });

  it('renders nothing at all when there is nothing to explain', () => {
    const { container } = render(<RefreshDetailsPanel rows={[]} />);
    expect(container.innerHTML).toBe('');
  });

  it('reveals every skipped and warned table WITH its reason, through the skippedDetail template', () => {
    render(<RefreshDetailsPanel rows={ROWS} />);

    // Collapsed by default: only the affordance is present.
    expect(screen.queryByText(strings.tableRefresh.detailsTitle)).toBeNull();

    fireEvent.click(screen.getByText(strings.tableRefresh.detailsShow));

    expect(screen.getByText(strings.tableRefresh.detailsTitle)).toBeTruthy();
    // The exact reason strings reach the user -- this is what "(see details)"
    // was promising and never delivering.
    expect(
      screen.getByText(templates.tableRefresh.skippedDetail('Orders', strings.tableRefresh.concurrentModificationSkip)),
    ).toBeTruthy();
    expect(
      screen.getByText(templates.tableRefresh.skippedDetail('Margins', strings.tableRefresh.lockBusySkip)),
    ).toBeTruthy();
    expect(
      screen.getByText(templates.tableRefresh.skippedDetail('Sales', strings.tableRefresh.provenanceWriteBackFailed)),
    ).toBeTruthy();

    // Skips and warnings are distinguished: a warned table WAS refreshed, and
    // must not read as a failure.
    expect(screen.getByText(strings.tableRefresh.detailsSkippedGroup)).toBeTruthy();
    expect(screen.getByText(strings.tableRefresh.detailsWarningGroup)).toBeTruthy();
  });

  it('collapses again on a second click', () => {
    render(<RefreshDetailsPanel rows={ROWS} />);
    fireEvent.click(screen.getByText(strings.tableRefresh.detailsShow));
    fireEvent.click(screen.getByText(strings.tableRefresh.detailsHide));
    expect(screen.queryByText(strings.tableRefresh.detailsTitle)).toBeNull();
  });

  it('omits a group entirely when it has no rows (no empty "Skipped" heading)', () => {
    const warnOnly = buildRefreshDetailRows({
      refreshed: ['Sales'], skipped: [], warnings: [{ name: 'Sales', reason: 'x' }],
    });
    render(<RefreshDetailsPanel rows={warnOnly} />);
    fireEvent.click(screen.getByText(strings.tableRefresh.detailsShow));
    expect(screen.queryByText(strings.tableRefresh.detailsSkippedGroup)).toBeNull();
    expect(screen.getByText(strings.tableRefresh.detailsWarningGroup)).toBeTruthy();
  });
});
