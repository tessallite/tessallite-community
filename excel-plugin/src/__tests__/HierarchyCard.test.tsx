/**
 * Hierarchy library levels regression (user live report 2026-07-08).
 *
 * The model-service LIST endpoint (HierarchySummaryResponse) sends
 * `level_names` + `level_count`, never full `levels` objects -- but the card
 * read only `hierarchy.levels`, so EVERY hierarchy in the Report Builder
 * library rendered as a bare header with no levels. These tests pin the
 * producer/consumer alignment: levels render from `level_names`, clicking a
 * synthesized level passes its NAME (the add handler resolves the bindable
 * dimension from the detail endpoint by name -- persona exclusions can skip
 * ordinals so a positional index is not a safe join key), the type chip
 * shows a human label instead of the raw "date_embedded" enum token, and
 * the Rows actions are real buttons (keyboard-reachable).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import HierarchyCard, { deriveDisplayLevels } from '../components/ReportBuilder/HierarchyCard';
import type { Hierarchy } from '../types/tessallite';

const SUMMARY_HIERARCHY: Hierarchy = {
  id: 'h-1',
  name: 'order_date Calendar',
  type: 'date_embedded',
  level_names: ['Year', 'Quarter', 'Month', 'Day'],
  level_count: 4,
};

describe('deriveDisplayLevels', () => {
  it('synthesizes display levels from the summary level_names when levels is absent', () => {
    const levels = deriveDisplayLevels(SUMMARY_HIERARCHY);
    expect(levels.map(l => l.name)).toEqual(['Year', 'Quarter', 'Month', 'Day']);
  });

  it('prefers full level objects when a caller has them', () => {
    const levels = deriveDisplayLevels({
      levels: [{ name: 'Year', level_number: 0, dimensionName: 'order_date_year' }],
      level_names: ['Ignored'],
    });
    expect(levels).toHaveLength(1);
    expect(levels[0].dimensionName).toBe('order_date_year');
  });

  it('returns empty for a hierarchy with neither field (renders header only, no crash)', () => {
    expect(deriveDisplayLevels({})).toEqual([]);
  });
});

describe('HierarchyCard summary rendering', () => {
  it('renders the levels from level_names when expanded (the regression: all hierarchies were header-only)', () => {
    render(<HierarchyCard hierarchy={SUMMARY_HIERARCHY} onAssignToRows={vi.fn()} />);

    fireEvent.click(screen.getByText('order_date Calendar'));

    for (const name of ['Year', 'Quarter', 'Month', 'Day']) {
      expect(screen.getByText(name)).toBeDefined();
    }
  });

  it('shows a human type label, not the raw enum token', () => {
    render(<HierarchyCard hierarchy={SUMMARY_HIERARCHY} onAssignToRows={vi.fn()} />);
    expect(screen.getByText('Calendar')).toBeDefined();
    expect(screen.queryByText('date_embedded')).toBeNull();
  });

  it('level Rows action is a real button passing the level NAME to the assign handler', () => {
    const onAssignToRows = vi.fn();
    render(<HierarchyCard hierarchy={SUMMARY_HIERARCHY} onAssignToRows={onAssignToRows} />);

    fireEvent.click(screen.getByText('order_date Calendar'));
    fireEvent.click(screen.getByRole('button', { name: 'Add order_date Calendar level Month to Rows' }));

    expect(onAssignToRows).toHaveBeenCalledTimes(1);
    expect(onAssignToRows.mock.calls[0][0]).toMatchObject({ name: 'Month' });
  });

  it('whole-hierarchy Rows action is a real button assigning with no level', () => {
    const onAssignToRows = vi.fn();
    render(<HierarchyCard hierarchy={SUMMARY_HIERARCHY} onAssignToRows={onAssignToRows} />);

    fireEvent.click(screen.getByRole('button', { name: 'Add order_date Calendar to Rows' }));

    expect(onAssignToRows).toHaveBeenCalledTimes(1);
    expect(onAssignToRows.mock.calls[0][0]).toBeUndefined();
  });
});
