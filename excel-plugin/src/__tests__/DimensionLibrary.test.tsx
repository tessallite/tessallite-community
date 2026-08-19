import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import DimensionLibrary from '../components/ReportBuilder/DimensionLibrary';
import type { Dimension } from '../types/tessallite';

const DIMENSIONS = [
  { id: 'dim-product', name: 'product', display_name: 'Product', data_type: 'string', source_type: 'dim' },
] as Dimension[];

describe('DimensionLibrary compatibility guidance', () => {
  it('disables incompatible dimensions and renders the backend reason', () => {
    const onAddToRows = vi.fn();
    render(
      <DimensionLibrary
        dimensions={DIMENSIONS}
        searchQuery=""
        onAddToRows={onAddToRows}
        onAddToColumns={vi.fn()}
        onAddToFilter={vi.fn()}
        onPreviewMembers={vi.fn()}
        expanded
        onToggleExpanded={vi.fn()}
        memberPreviewDimId={null}
        memberPreview={null}
        membersPreviewLoading={false}
        onCloseMemberPreview={vi.fn()}
        compatibilityByDimensionId={{
          'dim-product': {
            disabled: true,
            messages: ['Product cannot be used with the selected measure.'],
            compatibleDimensionNames: ['School'],
          },
        }}
      />,
    );

    expect(screen.getByRole('button', { name: /Add Product to Rows/i }).hasAttribute('disabled')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /Show details for Product/i }));
    expect(screen.getByText('Product cannot be used with the selected measure.')).toBeDefined();
    expect(screen.getByText(/Compatible dimensions: School/)).toBeDefined();

    fireEvent.click(screen.getByRole('button', { name: /Add Product to Rows/i }));
    expect(onAddToRows).not.toHaveBeenCalled();
  });
});

describe('DimensionLibrary member-preview close + header (Bug-6713 / Bug-6712)', () => {
  it('renders the member-preview close control as a real button wired to onCloseMemberPreview, and the header from the central string table', () => {
    const onCloseMemberPreview = vi.fn();
    render(
      <DimensionLibrary
        dimensions={DIMENSIONS}
        searchQuery=""
        onAddToRows={vi.fn()}
        onAddToColumns={vi.fn()}
        onAddToFilter={vi.fn()}
        onPreviewMembers={vi.fn()}
        expanded
        onToggleExpanded={vi.fn()}
        memberPreviewDimId="dim-product"
        memberPreview={{ members: [{ key: 'A', name: 'Alpha' }] }}
        membersPreviewLoading={false}
        onCloseMemberPreview={onCloseMemberPreview}
      />,
    );

    // Bug-6712: the header label comes from templates.library.dimensionsHeader.
    expect(screen.getByText('Dimensions (1)')).toBeDefined();

    // Bug-6713: the close control was a mouse-only span; it must be a real
    // button (focusable, Enter/Space activatable) with an accessible name.
    fireEvent.click(screen.getByRole('button', { name: 'Close member preview' }));
    expect(onCloseMemberPreview).toHaveBeenCalledTimes(1);
  });
});
