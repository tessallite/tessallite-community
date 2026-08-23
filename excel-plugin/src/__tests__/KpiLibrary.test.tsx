/**
 * Bug-6701 — the Report Builder "Add KPI" action (KpiCard's "+" icon, and
 * clicking an unchecked KPI row) must always reach the parent's
 * onAddKpiToValues handler with the full KPI object, for EVERY kpi shape --
 * including a custom/expression KPI with no value_measure_id (kpi_type
 * "custom" in the model; every KPI in the acme-demo seed is this shape).
 *
 * These tests pin the CHILD wiring (KpiLibrary -> KpiCard -> onAddToValues).
 * The parent-side decision of WHAT to do with a value_measure_id-less KPI
 * (route to the KPI-native formula insert instead of staging a pivot value)
 * is covered separately by zoneQuery.test.ts's planKpiZoneAdd suite -- that
 * pure function is what previously did nothing for this exact shape.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import KpiLibrary from '../components/ReportBuilder/KpiLibrary';
import type { Kpi } from '../types/tessallite';

function makeKpi(overrides: Partial<Kpi>): Kpi {
  return {
    id: 'kpi-1',
    name: 'net_margin_kpi',
    display_name: 'Net Margin',
    description: null,
    display_folder: null,
    value_measure_id: null,
    goal_measure_id: null,
    target_type: null,
    target_value: null,
    status_expression: null,
    trend_expression: null,
    status_graphic: 'Traffic Light',
    trend_graphic: 'Standard Arrow',
    weight: null,
    parent_kpi_id: null,
    certification_status: 'draft',
    replacement_id: null,
    owner_user_id: null,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function renderLibrary(
  kpi: Kpi,
  onAddKpiToValues = vi.fn(),
  overrides: {
    selectedKpiValueMeasureIds?: string[];
    onToggleKpi?: ReturnType<typeof vi.fn>;
    onToggleExpanded?: ReturnType<typeof vi.fn>;
    expanded?: boolean;
  } = {},
) {
  render(
    <KpiLibrary
      kpis={[kpi]}
      measures={[]}
      projectId="proj-1"
      modelId="model-1"
      personaId={null}
      searchQuery=""
      selectedKpiValueMeasureIds={overrides.selectedKpiValueMeasureIds ?? []}
      onToggleKpi={overrides.onToggleKpi ?? vi.fn()}
      onAddKpiToValues={onAddKpiToValues}
      expanded={overrides.expanded ?? true}
      onToggleExpanded={overrides.onToggleExpanded ?? vi.fn()}
    />,
  );
  return onAddKpiToValues;
}

describe('KpiLibrary "Add KPI" wiring (Bug-6701)', () => {
  it('invokes onAddKpiToValues with the KPI when a custom/expression KPI (no value_measure_id) is added via the "+" icon', () => {
    const kpi = makeKpi({ value_measure_id: null, goal_measure_id: null });
    const onAddKpiToValues = renderLibrary(kpi);

    // Bug-6708: the control must be a REAL button (focusable, Enter/Space
    // activatable, accessibly named per KPI) -- it was a bare clickable span,
    // unreachable for keyboard and screen-reader users.
    fireEvent.click(screen.getByRole('button', { name: 'Add KPI Net Margin value to report' }));

    expect(onAddKpiToValues).toHaveBeenCalledTimes(1);
    expect(onAddKpiToValues).toHaveBeenCalledWith(kpi);
  });

  it('invokes onAddKpiToValues with the KPI when a custom/expression KPI row itself is clicked (unchecked row shortcut)', () => {
    const kpi = makeKpi({ value_measure_id: null, goal_measure_id: null });
    const onAddKpiToValues = renderLibrary(kpi);

    fireEvent.click(screen.getByText('Net Margin'));

    expect(onAddKpiToValues).toHaveBeenCalledTimes(1);
    expect(onAddKpiToValues).toHaveBeenCalledWith(kpi);
  });

  it('also invokes onAddKpiToValues for a measure-backed KPI (regression guard: the fix must not break the working shape)', () => {
    const kpi = makeKpi({ value_measure_id: 'm-value', goal_measure_id: null });
    const onAddKpiToValues = renderLibrary(kpi);

    fireEvent.click(screen.getByRole('button', { name: 'Add KPI Net Margin value to report' }));

    expect(onAddKpiToValues).toHaveBeenCalledTimes(1);
    expect(onAddKpiToValues).toHaveBeenCalledWith(kpi);
  });
});

/**
 * Bug-6710 — the controls Bug-6708 made keyboard-operable must actually be
 * REACHABLE by keyboard: the section expander and the staged-KPI removal
 * were both mouse-only (a plain clickable Box / a row-click with the icon
 * cluster hidden).
 */
describe('KpiLibrary keyboard reachability (Bug-6710)', () => {
  it('exposes the section expand/collapse as a real button with expanded state', () => {
    const kpi = makeKpi({});
    const onToggleExpanded = vi.fn();
    renderLibrary(kpi, vi.fn(), { expanded: false, onToggleExpanded });

    const toggle = screen.getByRole('button', { name: 'Expand the KPIs section' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(onToggleExpanded).toHaveBeenCalledTimes(1);
  });

  it('exposes a per-KPI remove button when the KPI is staged (checked) instead of hiding all controls', () => {
    const kpi = makeKpi({ value_measure_id: 'm-value' });
    const onToggleKpi = vi.fn();
    renderLibrary(kpi, vi.fn(), {
      selectedKpiValueMeasureIds: ['m-value'],
      onToggleKpi,
    });

    fireEvent.click(screen.getByRole('button', { name: 'Remove KPI Net Margin value from report' }));
    expect(onToggleKpi).toHaveBeenCalledTimes(1);
    expect(onToggleKpi).toHaveBeenCalledWith(kpi);
  });
});
