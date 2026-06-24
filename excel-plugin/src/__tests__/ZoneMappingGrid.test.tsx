/**
 * L-1 / L-2 — Report Builder filter-edit client-side validation.
 *
 * gt/lt reach the source as a single scalar. The value must be valid for the
 * dimension's TYPE (Bug-1062): a numeric dim needs a number, a date dim needs a
 * parseable date (a date gt is server-valid SQL), text/unknown dims pass through
 * (lexicographic comparison is valid; genuine rejections surface as a readable
 * 502). Extra scalar values are silently dropped, so the dialog truncates with a
 * notice. inDateRange is SQL BETWEEN low AND high: reversed bounds silently
 * return empty, so the dialog auto-swaps with a notice. These tests pin the
 * type-aware guard, the single-value notice, the date-range swap, and that
 * cancelling after a notice reverts the already-applied filter.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import ZoneMappingGrid, { type ZoneItem } from '../components/ReportBuilder/ZoneMappingGrid';

// Numeric dimension (account id is a bigint) — gt/lt requires a number.
const FILTER_ITEM: ZoneItem = { id: 'dim-1', name: 'Account Id', zone: 'filters', operator: 'equals', values: [], data_type: 'bigint' };
// Date dimension — gt/lt requires a parseable date, NOT a number.
const DATE_FILTER_ITEM: ZoneItem = { id: 'dim-date', name: 'Business Date', zone: 'filters', operator: 'equals', values: [], data_type: 'date' };

function renderGrid(onUpdateFilter = vi.fn(), item: ZoneItem = FILTER_ITEM) {
  render(
    <ZoneMappingGrid
      items={[item]}
      onRemove={vi.fn()}
      onClear={vi.fn()}
      onInsertTable={vi.fn()}
      onOpenTemplates={vi.fn()}
      onUpdateFilter={onUpdateFilter}
    />,
  );
  return onUpdateFilter;
}

function renderGridWithCompatibilityBlock() {
  render(
    <ZoneMappingGrid
      items={[
        { id: 'measure-1', name: 'Revenue', zone: 'values' },
        { id: 'dim-product', name: 'Product', zone: 'rows' },
      ]}
      onRemove={vi.fn()}
      onClear={vi.fn()}
      onInsertTable={vi.fn()}
      onInsertChart={vi.fn()}
      onInsertLocalPivot={vi.fn()}
      onOpenTemplates={vi.fn()}
      compatibilityWarning={{
        title: 'Selected fields are not compatible',
        messages: ['Product with Revenue: Product has no join path to Revenue.'],
        compatibleDimensionNames: ['School'],
      }}
      insertDisabledReason="Remove incompatible fields before inserting this report."
    />,
  );
}

function openFilterDialog(name: RegExp = /Account Id/i) {
  // The filter chip is clickable; clicking opens the edit dialog.
  fireEvent.click(screen.getByText(name));
}

function setOperator(label: RegExp) {
  // The dialog has a single Select (Operator); MUI exposes it as a combobox.
  fireEvent.mouseDown(screen.getByRole('combobox'));
  const listbox = screen.getByRole('listbox');
  fireEvent.click(within(listbox).getByText(label));
}

function valueField(): HTMLElement {
  // The values TextField label switches between "Value (number)" and
  // "Values (comma-separated)" depending on the operator.
  return screen.getByRole('textbox');
}

describe('ZoneMappingGrid filter validation (L-1/L-2)', () => {
  it('rejects a non-numeric value for Greater Than and does not apply', () => {
    const onUpdate = renderGrid();
    openFilterDialog();
    setOperator(/^Greater Than$/);
    fireEvent.change(valueField(), { target: { value: 'abc' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(screen.getByText(/is not a number/i)).toBeDefined();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it('keeps a single value for Greater Than and notes the truncation', () => {
    const onUpdate = renderGrid();
    openFilterDialog();
    setOperator(/^Greater Than$/);
    fireEvent.change(valueField(), { target: { value: '100, 200' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(onUpdate).toHaveBeenCalledWith('dim-1', 'gt', ['100']);
    expect(screen.getByText(/single value/i)).toBeDefined();
  });

  it('applies a valid numeric Greater Than value', () => {
    const onUpdate = renderGrid();
    openFilterDialog();
    setOperator(/^Greater Than$/);
    fireEvent.change(valueField(), { target: { value: '100' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(onUpdate).toHaveBeenCalledWith('dim-1', 'gt', ['100']);
  });

  it('auto-swaps a reversed Date Range and notes the reorder', () => {
    const onUpdate = renderGrid();
    openFilterDialog();
    setOperator(/^Date Range$/);
    fireEvent.change(valueField(), { target: { value: '2025-12-31, 2025-01-01' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(onUpdate).toHaveBeenCalledWith('dim-1', 'inDateRange', ['2025-01-01', '2025-12-31']);
    expect(screen.getByText(/reordered/i)).toBeDefined();
  });

  it('leaves an in-order Date Range untouched and closes', () => {
    const onUpdate = renderGrid();
    openFilterDialog();
    setOperator(/^Date Range$/);
    fireEvent.change(valueField(), { target: { value: '2025-01-01, 2025-12-31' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(onUpdate).toHaveBeenCalledWith('dim-1', 'inDateRange', ['2025-01-01', '2025-12-31']);
    expect(screen.queryByText(/reordered/i)).toBeNull();
  });

  // Bug-1062: a Greater Than on a DATE dimension is server-valid SQL
  // (business_date > '2025-06-01' → 159,683,175.08 live). The round-2 numeric
  // gate wrongly rejected it as "not a number"; the type-aware gate must apply it.
  it('applies a Greater Than date on a date dimension (no numeric rejection)', () => {
    const onUpdate = renderGrid(vi.fn(), DATE_FILTER_ITEM);
    openFilterDialog(/Business Date/i);
    setOperator(/^Greater Than$/);
    fireEvent.change(valueField(), { target: { value: '2025-06-01' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(screen.queryByText(/is not a number/i)).toBeNull();
    expect(onUpdate).toHaveBeenCalledWith('dim-date', 'gt', ['2025-06-01']);
  });

  it('rejects a non-date value for Greater Than on a date dimension', () => {
    const onUpdate = renderGrid(vi.fn(), DATE_FILTER_ITEM);
    openFilterDialog(/Business Date/i);
    setOperator(/^Greater Than$/);
    fireEvent.change(valueField(), { target: { value: 'not-a-date' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(screen.getByText(/is not a date/i)).toBeDefined();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  // INFO-2: after a truncation/swap notice the filter is already committed.
  // Cancel must revert it to the operator/values it had when the dialog opened.
  it('reverts the applied filter when Cancel is clicked after a notice', () => {
    // The item opens with equals/[] — applying gt with two values truncates and
    // shows a notice (the filter is now committed as gt/[100]); Cancel reverts.
    const onUpdate = renderGrid();
    openFilterDialog();
    setOperator(/^Greater Than$/);
    fireEvent.change(valueField(), { target: { value: '100, 200' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    // First call: the truncated apply.
    expect(onUpdate).toHaveBeenNthCalledWith(1, 'dim-1', 'gt', ['100']);
    expect(screen.getByText(/single value/i)).toBeDefined();

    // Cancel after the notice reverts to the pre-edit equals/[] state.
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(onUpdate).toHaveBeenNthCalledWith(2, 'dim-1', 'equals', []);
  });

  it('keeps the applied filter when Done is clicked after a notice', () => {
    const onUpdate = renderGrid();
    openFilterDialog();
    setOperator(/^Greater Than$/);
    fireEvent.change(valueField(), { target: { value: '100, 200' } });
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(onUpdate).toHaveBeenNthCalledWith(1, 'dim-1', 'gt', ['100']);
    // Done closes without a second (reverting) call.
    fireEvent.click(screen.getByRole('button', { name: /done/i }));
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it('shows compatibility guidance and disables insert actions while incompatible fields remain', () => {
    renderGridWithCompatibilityBlock();

    expect(screen.getByText('Selected fields are not compatible')).toBeDefined();
    expect(screen.getByText(/Product has no join path to Revenue/)).toBeDefined();
    expect(screen.getByText(/Compatible dimensions: School/)).toBeDefined();
    expect(screen.getByRole('button', { name: /table/i }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: /chart/i }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: /pivot/i }).hasAttribute('disabled')).toBe(true);
  });
});
