import { useState, useCallback } from 'react';
import {
  Box,
  Typography,
  Chip,
  Alert,
  Button,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  IconButton,
  Tooltip,
} from '@mui/material';
import {
  DeleteSweepOutlined,
  GridViewOutlined,
  InsertChartOutlined,
  PivotTableChartOutlined,
  TableChartOutlined,
  TuneOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Zone } from '../../types/tessallite';
import { classifyDataType } from '../../utils/dataTypes';

export interface ZoneItem {
  id: string;
  name: string;
  zone: Zone;
  operator?: string;
  values?: string[];
  /**
   * The dimension's physical data-type spelling (e.g. "bigint", "date",
   * "character varying"). Threaded in when the field is added so the filter
   * dialog can gate gt/lt validation by semantic category rather than assuming
   * every comparison is numeric (a date/text gt is server-valid).
   */
  data_type?: string;
  /**
   * Kind of zone item. Defaults to a measure/dimension field. Named-set and
   * hierarchy-level items are NOT directly bindable by their UUID token, so
   * F-025-11 resolves them at add-time into a bindable dimension (+ member
   * list for named sets) and stores the resolution here.
   */
  kind?: 'field' | 'named_set' | 'hierarchy_level';
  /**
   * The technical dimension name this item binds to. For a named set this is
   * the set's underlying dimension; for a hierarchy level it is the level's
   * key-attribute dimension. The query builder uses this on the axis instead
   * of the raw UUID token (F-025-11).
   */
  bindDimension?: string;
  /**
   * For a named set: the member keys that define the set. The query builder
   * emits an `in` filter over these on `bindDimension` (F-025-11).
   */
  memberKeys?: string[];
}

interface ZoneMappingGridProps {
  items: ZoneItem[];
  onRemove: (id: string) => void;
  onClear: () => void;
  onInsertTable: () => void;
  onInsertChart?: () => void;
  onInsertLocalPivot?: () => void;
  onOpenTemplates: () => void;
  onUpdateFilter?: (id: string, operator: string, values: string[]) => void;
  compatibilityWarning?: {
    title: string;
    messages: string[];
    compatibleDimensionNames?: string[];
  } | null;
  insertDisabledReason?: string | null;
}

function ChipList({
  items,
  onRemove,
  onChipClick,
}: {
  items: ZoneItem[];
  onRemove: (id: string) => void;
  onChipClick?: (item: ZoneItem) => void;
}) {
  if (items.length === 0) {
    return (
      <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
        Empty
      </Typography>
    );
  }

  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, minWidth: 0 }}>
      {items.map(item => (
        <Chip
          key={item.id}
          label={item.operator ? `${item.name} ${item.operator} ${(item.values || []).join(', ') || '...'}` : item.name}
          size="small"
          onDelete={() => onRemove(item.id)}
          onClick={() => onChipClick?.(item)}
          sx={{
            maxWidth: '100%',
            fontSize: 11,
            fontWeight: 600,
            height: 24,
            bgcolor: tokens.colorPrimaryBg,
            color: tokens.colorPrimary,
            borderRadius: 1,
            '& .MuiChip-deleteIcon': { fontSize: 14, color: tokens.colorPrimary },
            '& .MuiChip-label': { overflow: 'hidden', textOverflow: 'ellipsis' },
            cursor: onChipClick ? 'pointer' : 'default',
          }}
        />
      ))}
    </Box>
  );
}

export default function ZoneMappingGrid({
  items,
  onRemove,
  onClear,
  onInsertTable,
  onInsertChart,
  onInsertLocalPivot,
  onOpenTemplates,
  onUpdateFilter,
  compatibilityWarning,
  insertDisabledReason,
}: ZoneMappingGridProps) {
  const filters = items.filter(i => i.zone === 'filters');
  const columns = items.filter(i => i.zone === 'columns');
  const values = items.filter(i => i.zone === 'values');
  const rows = items.filter(i => i.zone === 'rows');
  const hasItems = items.length > 0;
  const canInsert = values.length > 0;
  const insertBlocked = Boolean(insertDisabledReason);

  const [editingFilter, setEditingFilter] = useState<ZoneItem | null>(null);
  const [editOperator, setEditOperator] = useState('equals');
  const [editValues, setEditValues] = useState('');
  const [editError, setEditError] = useState<string | null>(null);
  const [editNotice, setEditNotice] = useState<string | null>(null);

  const isScalarOperator = editOperator === 'gt' || editOperator === 'lt';
  // Label the scalar input by the dimension's type so the analyst knows what to
  // enter: numeric/boolean dims expect a number, date dims a date, text dims a value.
  const scalarCategory = classifyDataType(editingFilter?.data_type);
  const scalarValueKind =
    scalarCategory === 'date' ? 'date'
      : scalarCategory === 'text' || scalarCategory === 'unknown' ? 'value'
        : 'number';

  const handleOpenFilterEdit = useCallback((item: ZoneItem) => {
    setEditingFilter(item);
    setEditOperator(item.operator || 'equals');
    setEditValues((item.values || []).join(', '));
    setEditError(null);
    setEditNotice(null);
  }, []);

  // INFO-2: once a notice is shown the (swapped/truncated) filter has ALREADY
  // been committed via onUpdateFilter. Closing the dialog must distinguish Done
  // (keep) from Cancel (revert to the operator/values the filter had on open).
  // editingFilter holds the pre-edit snapshot, so reverting is a re-commit of it.
  const closeFilterDialog = useCallback((revert: boolean) => {
    if (revert && editNotice && editingFilter && onUpdateFilter) {
      onUpdateFilter(editingFilter.id, editingFilter.operator || 'equals', editingFilter.values || []);
    }
    setEditingFilter(null);
    setEditError(null);
    setEditNotice(null);
  }, [editNotice, editingFilter, onUpdateFilter]);

  const handleSaveFilter = useCallback(() => {
    if (!editingFilter || !onUpdateFilter) {
      setEditingFilter(null);
      return;
    }
    let values = editValues.split(',').map(v => v.trim()).filter(v => v);
    let notice: string | null = null;

    // L-1 (Bug-1062): gt/lt compare against a single scalar. The value must be
    // valid for the dimension's TYPE — not unconditionally numeric. A numeric
    // dim needs a number; a date dim needs a parseable date; text dims compare
    // lexicographically (server-valid SQL). The round-2 fix gated every gt/lt as
    // numeric, which wrongly rejected server-valid `gt business_date '2025-06-01'`.
    // Gate by the semantic category of the dimension's physical data_type.
    if ((editOperator === 'gt' || editOperator === 'lt')) {
      if (values.length === 0) {
        setEditError('Enter a value to compare against.');
        return;
      }
      const category = classifyDataType(editingFilter.data_type);
      if (category === 'numeric' || category === 'boolean') {
        if (!/^-?\d+(\.\d+)?$/.test(values[0])) {
          setEditError(`"${values[0]}" is not a number. Greater Than / Less Than need a numeric value here.`);
          return;
        }
      } else if (category === 'date') {
        if (Number.isNaN(Date.parse(values[0]))) {
          setEditError(`"${values[0]}" is not a date. Greater Than / Less Than need a date here (e.g. 2025-06-01).`);
          return;
        }
      }
      // text / unknown: no client gate — lexicographic comparison is valid SQL,
      // and a genuine source rejection surfaces as a readable 502 detail.

      // Scalar operators bind a single value server-side; extra values are
      // silently ignored. Keep only the first and tell the analyst.
      if (values.length > 1) {
        notice = `Greater Than / Less Than use a single value — using "${values[0]}".`;
        values = [values[0]];
      }
    }

    // L-2: inDateRange is SQL BETWEEN low AND high; reversed bounds silently
    // return an empty result. Auto-swap so the analyst gets the range they
    // obviously meant, and tell them it was reordered rather than failing blank.
    if (editOperator === 'inDateRange' && values.length === 2) {
      const [a, b] = values;
      const da = Date.parse(a);
      const db = Date.parse(b);
      if (!Number.isNaN(da) && !Number.isNaN(db) && da > db) {
        values = [b, a];
        notice = `Dates were entered high-to-low — reordered to ${b} … ${a}.`;
      }
    }

    onUpdateFilter(editingFilter.id, editOperator, values);
    setEditError(null);
    if (notice) {
      setEditNotice(notice);
    } else {
      setEditingFilter(null);
    }
  }, [editingFilter, editOperator, editValues, onUpdateFilter]);

  const zoneRows: Array<{ label: string; hint: string; zoneItems: ZoneItem[]; editable?: boolean }> = [
    { label: 'Values', hint: 'Add measures or KPIs', zoneItems: values },
    { label: 'Rows', hint: 'Add dimensions', zoneItems: rows },
    { label: 'Columns', hint: 'Optional split', zoneItems: columns },
    { label: 'Filters', hint: 'Optional criteria', zoneItems: filters, editable: true },
  ];

  return (
    <Box sx={{ borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: tokens.colorWhite }}>
      <Box sx={{ p: 1.25 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.75, gap: 0.75 }}>
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, lineHeight: 1.2 }}>
              Build report
            </Typography>
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, lineHeight: 1.2 }}>
              Pick fields, then insert into Excel.
            </Typography>
          </Box>
          {hasItems && (
            <Tooltip title="Clear layout">
              <IconButton size="small" onClick={onClear} aria-label="Clear report layout" sx={{ color: tokens.colorTextSecondary }}>
                <DeleteSweepOutlined sx={{ fontSize: 18 }} />
              </IconButton>
            </Tooltip>
          )}
          <Button
            size="small"
            variant="text"
            startIcon={<GridViewOutlined sx={{ fontSize: 16 }} />}
            onClick={onOpenTemplates}
            sx={{ fontSize: 11, minWidth: 'auto', textTransform: 'none', color: tokens.colorPrimary, px: 0.75 }}
          >
            Templates
          </Button>
        </Box>

        <Box sx={{ display: 'grid', gap: 0.5 }}>
          {zoneRows.map(zone => (
            <Box
              key={zone.label}
              sx={{
                display: 'grid',
                gridTemplateColumns: '70px minmax(0, 1fr)',
                alignItems: 'start',
                gap: 0.75,
                minHeight: 32,
                px: 0.75,
                py: 0.6,
                border: `1px solid ${tokens.colorBorderLight}`,
                borderRadius: 1,
                bgcolor: zone.zoneItems.length > 0 ? tokens.colorWhite : tokens.colorSubtleFill,
              }}
            >
              <Box>
                <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', lineHeight: 1.2 }}>
                  {zone.label}
                </Typography>
                {zone.zoneItems.length === 0 && (
                  <Typography sx={{ fontSize: 10, color: tokens.colorMuted, lineHeight: 1.2 }}>
                    {zone.hint}
                  </Typography>
                )}
              </Box>
              <ChipList
                items={zone.zoneItems}
                onRemove={onRemove}
                onChipClick={zone.editable ? handleOpenFilterEdit : undefined}
              />
            </Box>
          ))}
        </Box>

        {compatibilityWarning && (
          <Alert severity="warning" sx={{ mt: 0.75, py: 0.5, '& .MuiAlert-message': { minWidth: 0 } }}>
            <Typography sx={{ fontSize: 11, fontWeight: 700, lineHeight: 1.25 }}>
              {compatibilityWarning.title}
            </Typography>
            {compatibilityWarning.messages.slice(0, 3).map(message => (
              <Typography key={message} sx={{ fontSize: 10.5, lineHeight: 1.3 }}>
                {message}
              </Typography>
            ))}
            {compatibilityWarning.compatibleDimensionNames && compatibilityWarning.compatibleDimensionNames.length > 0 && (
              <Typography sx={{ fontSize: 10.5, lineHeight: 1.3 }}>
                Compatible dimensions: {compatibilityWarning.compatibleDimensionNames.join(', ')}
              </Typography>
            )}
          </Alert>
        )}

        {filters.length > 0 && (
          <Box sx={{ mt: 0.5, display: 'flex', alignItems: 'center', gap: 0.5, color: tokens.colorTextSecondary }}>
            <TuneOutlined sx={{ fontSize: 13 }} />
            <Typography sx={{ fontSize: 10 }}>
              Select a filter chip to set operator and values.
            </Typography>
          </Box>
        )}

        <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 0.5, mt: 1 }}>
          <Button
            size="small"
            variant="contained"
            startIcon={<TableChartOutlined sx={{ fontSize: 16 }} />}
            onClick={onInsertTable}
            disabled={!canInsert || insertBlocked}
            title={insertDisabledReason ?? undefined}
            sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
          >
            Table
          </Button>
          <Button
            size="small"
            variant="outlined"
            startIcon={<InsertChartOutlined sx={{ fontSize: 16 }} />}
            onClick={onInsertChart}
            disabled={!canInsert || !onInsertChart || insertBlocked}
            title={insertDisabledReason ?? undefined}
            sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
          >
            Chart
          </Button>
          <Button
            size="small"
            variant="outlined"
            startIcon={<PivotTableChartOutlined sx={{ fontSize: 16 }} />}
            onClick={onInsertLocalPivot}
            disabled={!canInsert || !onInsertLocalPivot || insertBlocked}
            title={insertDisabledReason ?? undefined}
            sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
          >
            Pivot
          </Button>
        </Box>

        {!canInsert && (
          <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mt: 0.5 }}>
            Add at least one measure to enable insert actions.
          </Typography>
        )}
        {canInsert && insertDisabledReason && (
          <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mt: 0.5 }}>
            {insertDisabledReason}
          </Typography>
        )}
      </Box>

      <Dialog open={Boolean(editingFilter)} onClose={() => closeFilterDialog(true)} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 300, borderRadius: 2 } }}>
        <DialogTitle sx={{ fontSize: 14, fontWeight: 700, pb: 0 }}>
          Filter: {editingFilter?.name}
        </DialogTitle>
        <DialogContent sx={{ p: 2 }}>
          <FormControl fullWidth size="small" sx={{ mb: 2 }}>
            <InputLabel>Operator</InputLabel>
            <Select value={editOperator} label="Operator" onChange={e => { setEditOperator(e.target.value); setEditError(null); setEditNotice(null); }}>
              <MenuItem value="equals">Equals</MenuItem>
              <MenuItem value="notEquals">Not Equals</MenuItem>
              <MenuItem value="contains">Contains</MenuItem>
              <MenuItem value="notContains">Not Contains</MenuItem>
              <MenuItem value="gt">Greater Than</MenuItem>
              <MenuItem value="lt">Less Than</MenuItem>
              <MenuItem value="inDateRange">Date Range</MenuItem>
              <MenuItem value="set">In Set</MenuItem>
            </Select>
          </FormControl>
          <TextField
            fullWidth
            size="small"
            label={isScalarOperator ? `Value (${scalarValueKind})` : 'Values (comma-separated)'}
            value={editValues}
            onChange={e => { setEditValues(e.target.value); setEditError(null); setEditNotice(null); }}
            error={Boolean(editError)}
            helperText={
              editError
                ? editError
                : isScalarOperator
                  ? `Enter a single ${scalarValueKind}`
                  : editOperator === 'inDateRange'
                    ? 'Enter two dates: start, end'
                    : 'Enter values separated by commas'
            }
          />
          {editNotice && (
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mt: 1 }}>
              {editNotice}
            </Typography>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 2, pb: 1.5 }}>
          <Button size="small" onClick={() => closeFilterDialog(true)} sx={{ textTransform: 'none' }}>Cancel</Button>
          <Button
            size="small"
            variant="contained"
            onClick={editNotice ? () => closeFilterDialog(false) : handleSaveFilter}
            sx={{ textTransform: 'none' }}
          >
            {editNotice ? 'Done' : 'Apply'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
