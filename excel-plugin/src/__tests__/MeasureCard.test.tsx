/**
 * Bug-6713 — a staged (checked) measure previously hid its whole icon
 * cluster, so un-staging was row-click-only: unreachable for keyboard and
 * screen-reader users. The checked state must expose a real remove button
 * (the keyboard counterpart of the row click), mirroring KpiCard's
 * Bug-6710 remove control.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import MeasureCard from '../components/ReportBuilder/MeasureCard';
import type { Measure } from '../types/tessallite';

const MEASURE = {
  id: 'm-1',
  name: 'net_sales',
  display_name: 'Net Sales',
  default_agg: 'sum',
  measure_type: 'standard',
} as Measure;

describe('MeasureCard staged-state removal (Bug-6713)', () => {
  it('exposes a real remove button when checked, wired to onToggle', () => {
    const onToggle = vi.fn();
    render(
      <MeasureCard
        measure={MEASURE}
        checked
        onToggle={onToggle}
        onAddToValues={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Remove Net Sales from Values' }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('still exposes the add button (not the remove button) when unchecked', () => {
    const onAddToValues = vi.fn();
    render(
      <MeasureCard
        measure={MEASURE}
        checked={false}
        onToggle={vi.fn()}
        onAddToValues={onAddToValues}
      />,
    );

    expect(screen.queryByRole('button', { name: 'Remove Net Sales from Values' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Add Net Sales to Values' }));
    expect(onAddToValues).toHaveBeenCalledTimes(1);
  });
});

/**
 * Bug-9747 -- MeasureCard must expose an "Add to Filter" button when the
 * onAddToFilter callback is provided, enabling HAVING-style measure-value
 * filters (e.g. SUM(base_amount) > 10000). Previously MeasureLibrary had
 * no filter affordance at all, unlike DimensionLibrary and NamedSetLibrary.
 */
describe('MeasureCard add-to-filter affordance (Bug-9747)', () => {
  it('shows an "Add to Filter" button when onAddToFilter is provided and unchecked', () => {
    const onAddToFilter = vi.fn();
    render(
      <MeasureCard
        measure={MEASURE}
        checked={false}
        onToggle={vi.fn()}
        onAddToValues={vi.fn()}
        onAddToFilter={onAddToFilter}
      />,
    );

    const filterBtn = screen.getByRole('button', { name: 'Add Net Sales to Filter' });
    expect(filterBtn).toBeTruthy();
    fireEvent.click(filterBtn);
    expect(onAddToFilter).toHaveBeenCalledTimes(1);
  });

  it('does not show the filter button when onAddToFilter is not provided', () => {
    render(
      <MeasureCard
        measure={MEASURE}
        checked={false}
        onToggle={vi.fn()}
        onAddToValues={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: 'Add Net Sales to Filter' })).toBeNull();
  });

  it('keeps the filter button visible and clickable when the measure is checked (staged in Values) -- live-reproduced regression: a measure must be filterable while also being a value, the exact SQL pattern SUM(x) ... HAVING SUM(x) > n requires', () => {
    const onAddToFilter = vi.fn();
    render(
      <MeasureCard
        measure={MEASURE}
        checked
        onToggle={vi.fn()}
        onAddToValues={vi.fn()}
        onAddToFilter={onAddToFilter}
      />,
    );

    const filterBtn = screen.getByRole('button', { name: 'Add Net Sales to Filter' });
    expect(filterBtn).toBeTruthy();
    fireEvent.click(filterBtn);
    expect(onAddToFilter).toHaveBeenCalledTimes(1);
    // The remove button (Bug-6713) must still be present alongside it.
    expect(screen.getByRole('button', { name: 'Remove Net Sales from Values' })).toBeTruthy();
  });
});

/**
 * Bug-9759-followup -- same regression class as Bug-9747, this time for the
 * "insert as formula" (sigma/Functions icon) button: it must stay visible
 * and clickable once the measure is checked (staged in Values), not just
 * while unchecked. Live-reproduced: staging a measure in table Values made
 * its formula-insert icon disappear entirely.
 */
describe('MeasureCard insert-as-function affordance stays visible when checked', () => {
  it('keeps the insert-as-function button visible and clickable when unchecked', () => {
    const onInsertAsFunction = vi.fn();
    render(
      <MeasureCard
        measure={MEASURE}
        checked={false}
        onToggle={vi.fn()}
        onAddToValues={vi.fn()}
        onInsertAsFunction={onInsertAsFunction}
      />,
    );

    const fnBtn = screen.getByRole('button', { name: 'Insert Net Sales as formula' });
    fireEvent.click(fnBtn);
    expect(onInsertAsFunction).toHaveBeenCalledTimes(1);
  });

  it('keeps the insert-as-function button visible and clickable when the measure is checked (staged in Values)', () => {
    const onInsertAsFunction = vi.fn();
    render(
      <MeasureCard
        measure={MEASURE}
        checked
        onToggle={vi.fn()}
        onAddToValues={vi.fn()}
        onInsertAsFunction={onInsertAsFunction}
      />,
    );

    const fnBtn = screen.getByRole('button', { name: 'Insert Net Sales as formula' });
    fireEvent.click(fnBtn);
    expect(onInsertAsFunction).toHaveBeenCalledTimes(1);
    // The remove button (Bug-6713) must still be present alongside it.
    expect(screen.getByRole('button', { name: 'Remove Net Sales from Values' })).toBeTruthy();
  });
});
