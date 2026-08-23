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
