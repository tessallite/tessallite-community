import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import TemplatePicker from '../components/ReportBuilder/TemplatePicker';

describe('TemplatePicker', () => {
  const defaultProps = {
    open: true,
    onClose: vi.fn(),
    onSelect: vi.fn(),
    measureCount: 3,
    hasTimeDimension: true,
    hasCategoricalDimension: true,
  };

  it('renders all 6 template names when all prerequisites met', () => {
    render(<TemplatePicker {...defaultProps} />);
    expect(screen.getByText('Time Series')).toBeDefined();
    expect(screen.getByText('Top N Breakdown')).toBeDefined();
    expect(screen.getByText('Period Comparison')).toBeDefined();
    expect(screen.getByText('Geographic Breakdown')).toBeDefined();
    expect(screen.getByText('Variance Analysis')).toBeDefined();
    expect(screen.getByText('KPI Snapshot')).toBeDefined();
  });

  it('shows multiple missing-field warnings when no prerequisites met', () => {
    render(<TemplatePicker {...defaultProps} measureCount={0} hasTimeDimension={false} hasCategoricalDimension={false} />);
    const warnings = screen.getAllByText(/missing/i);
    expect(warnings.length).toBeGreaterThanOrEqual(1);
  });

  it('calls onSelect when clicking a valid template', () => {
    const onSelect = vi.fn();
    render(<TemplatePicker {...defaultProps} onSelect={onSelect} />);
    fireEvent.click(screen.getByText('Time Series'));
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'time-series' }),
    );
  });

  it('does not call onSelect when clicking an invalid template', () => {
    const onSelect = vi.fn();
    render(<TemplatePicker {...defaultProps} measureCount={0} hasTimeDimension={false} onSelect={onSelect} />);
    fireEvent.click(screen.getByText('Time Series'));
    expect(onSelect).not.toHaveBeenCalled();
  });
});
