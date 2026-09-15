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
  Menu,
} from '@mui/material';
import {
  DeleteSweepOutlined,
  AccountTreeOutlined,
  FunctionsOutlined,
  GridViewOutlined,
  InsertChartOutlined,
  ManageSearchOutlined,
  PivotTableChartOutlined,
  RefreshOutlined,
  TableChartOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import type { Zone } from '../../types/tessallite';
import { classifyDataType } from '../../utils/dataTypes';
import { strings, templates } from '../../i18n/strings';

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

interface ZoneChipCompatibility {
  disabled: boolean;
  messages: string[];
}

interface ZoneMappingGridProps {
  items: ZoneItem[];
  // Bug-6358: remove is zone-qualified. The same field id can sit in two zones
  // at once (e.g. a dimension on Rows AND as a Filter); deleting one chip must
  // only remove that zone's item, not every item sharing the id.
  onRemove: (id: string, zone: Zone) => void;
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
  /** Optional compact toolbar actions owned by ReportBuilder. */
  onRefreshValues?: () => void;
  onRefreshSheetData?: () => void;
  refreshSheetDataLoading?: boolean;
  onOpenCubeWizard?: () => void;
  onOpenConnectionWizard?: () => void;
  onOpenTrace?: () => void;
  hasLastQuery?: boolean;
  insertMode?: 'live' | 'static';
  onInsertModeChange?: (mode: 'live' | 'static') => void;
  compatibilityByDimensionId?: Record<string, ZoneChipCompatibility>;
}

function ChipList({
  items,
  onRemove,
  onChipClick,
  compatibilityByDimensionId,
}: {
  items: ZoneItem[];
  onRemove: (id: string, zone: Zone) => void;
  onChipClick?: (item: ZoneItem) => void;
  compatibilityByDimensionId?: Record<string, ZoneChipCompatibility>;
}) {
  if (items.length === 0) {
    return (
      <Typography sx={{ fontSize: 11, color: '#9a9a9a' }}>
        —
      </Typography>
    );
  }

  return (
    <Box sx={{ display: 'flex', flexWrap: 'nowrap', gap: 0.5, minWidth: 0, overflowX: 'auto', overflowY: 'hidden', scrollbarWidth: 'thin' }}>
      {items.map(item => {
        const compatibility = compatibilityByDimensionId?.[item.bindDimension || item.id];
        const incompatible = Boolean(compatibility?.disabled);
        const isFilter = item.zone === 'filters';
        const chip = (
          <Chip
            label={item.operator ? `${item.name} ${item.operator} ${(item.values || []).join(', ') || '...'}` : item.name}
            size="small"
            onDelete={() => onRemove(item.id, item.zone)}
            onClick={() => onChipClick?.(item)}
            sx={{
              maxWidth: '100%',
              fontSize: 11,
              fontWeight: 600,
              height: 18,
              bgcolor: incompatible ? tokens.colorRedBg : isFilter ? tokens.colorPrimaryBg : tokens.colorWhite,
              color: incompatible ? tokens.colorRed : isFilter ? tokens.colorPrimary : tokens.colorTextSecondary,
              borderRadius: 0.5,
              border: incompatible
                ? `1px solid ${tokens.colorRed}`
                : isFilter
                  ? `1px dashed ${tokens.colorPrimary}`
                  : `1px solid ${tokens.colorBorder}`,
              flexShrink: 0,
              whiteSpace: 'nowrap',
              '& .MuiChip-deleteIcon': { fontSize: 14, color: incompatible ? tokens.colorRed : isFilter ? tokens.colorPrimary : tokens.colorTextSecondary },
              '& .MuiChip-label': { overflow: 'hidden', textOverflow: 'ellipsis' },
              cursor: onChipClick ? 'pointer' : 'default',
            }}
          />
        );
        return incompatible ? (
          <Tooltip key={`${item.zone}:${item.id}`} title={compatibility?.messages[0] || strings.reportBuilder.incompatibleFields}>
            <span style={{ display: 'inline-flex', flexShrink: 0, minWidth: 0 }}>{chip}</span>
          </Tooltip>
        ) : (
          <span key={`${item.zone}:${item.id}`} style={{ display: 'inline-flex', flexShrink: 0, minWidth: 0 }}>{chip}</span>
        );
      })}
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
  onRefreshValues,
  onRefreshSheetData,
  refreshSheetDataLoading = false,
  onOpenCubeWizard,
  onOpenConnectionWizard,
  onOpenTrace,
  hasLastQuery = false,
  insertMode,
  onInsertModeChange,
  compatibilityByDimensionId,
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
  const [refreshMenuAnchor, setRefreshMenuAnchor] = useState<HTMLElement | null>(null);

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
        setEditError(strings.filter.enterValueToCompare);
        return;
      }
      const category = classifyDataType(editingFilter.data_type);
      if (category === 'numeric' || category === 'boolean') {
        if (!/^-?\d+(\.\d+)?$/.test(values[0])) {
          setEditError(templates.filter.notANumber(values[0]));
          return;
        }
      } else if (category === 'date') {
        if (Number.isNaN(Date.parse(values[0]))) {
          setEditError(templates.filter.notADate(values[0]));
          return;
        }
      }
      // text / unknown: no client gate — lexicographic comparison is valid SQL,
      // and a genuine source rejection surfaces as a readable 502 detail.

      // Scalar operators bind a single value server-side; extra values are
      // silently ignored. Keep only the first and tell the analyst.
      if (values.length > 1) {
        notice = templates.filter.scalarTruncated(values[0]);
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
        notice = templates.filter.datesReordered(b, a);
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
    { label: strings.zone.valuesLabel, hint: strings.zone.valuesHint, zoneItems: values },
    { label: strings.zone.rowsLabel, hint: strings.zone.rowsHint, zoneItems: rows },
    { label: strings.zone.columnsLabel, hint: strings.zone.columnsHint, zoneItems: columns },
    { label: strings.zone.filtersLabel, hint: strings.zone.filtersHint, zoneItems: filters, editable: true },
  ];

  const toolbarButton = (
    label: string,
    icon: React.ReactNode,
    onClick: (() => void) | undefined,
    options: { contained?: boolean; disabled?: boolean; disabledReason?: string } = {},
  ) => (
    <Tooltip key={label} title={options.disabledReason || label}>
      <span>
        <IconButton
          size="small"
          aria-label={label}
          title={label}
          onClick={onClick}
          disabled={options.disabled || !onClick}
          sx={{
            width: 26,
            height: 24,
            flexShrink: 0,
            borderRadius: 0.5,
            color: options.contained ? tokens.colorWhite : tokens.colorTextSecondary,
            bgcolor: options.contained ? tokens.colorPrimary : 'transparent',
            border: options.contained ? 'none' : `1px solid ${tokens.colorBorder}`,
            '&:hover': {
              bgcolor: options.contained ? tokens.colorPrimaryDark : tokens.colorPrimaryBg,
              color: options.contained ? tokens.colorWhite : tokens.colorPrimary,
            },
            '&.Mui-disabled': { color: '#b0b0b0', borderColor: tokens.colorBorderLight },
          }}
        >
          {icon}
        </IconButton>
      </span>
    </Tooltip>
  );

  return (
    <Box sx={{ borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: tokens.colorWhite }}>
      <Box sx={{ px: 1.25, pt: 0.75, pb: 0.5, display: 'grid', gap: 0.375 }}>
        {zoneRows.map(zone => (
          <Box
            key={zone.label}
            sx={{
              height: 26,
              minHeight: 26,
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              px: 1,
              border: `1px solid ${tokens.colorBorderLight}`,
              borderRadius: 0.5,
              bgcolor: tokens.colorSubtleFill,
            }}
          >
            <Tooltip title={zone.hint} placement="left">
              <Typography sx={{ width: 56, flexShrink: 0, fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', letterSpacing: '0.03em', lineHeight: 1 }}>
                {zone.label}
              </Typography>
            </Tooltip>
            <Box sx={{ minWidth: 0, flex: 1, display: 'flex', alignItems: 'center', overflow: 'hidden' }}>
              <ChipList
                items={zone.zoneItems}
                onRemove={onRemove}
                onChipClick={zone.editable ? handleOpenFilterEdit : undefined}
                compatibilityByDimensionId={compatibilityByDimensionId}
              />
            </Box>
            {zone.zoneItems === values && hasItems && (
              <Tooltip title={strings.zone.clearLayout}>
                <IconButton
                  size="small"
                  onClick={onClear}
                  aria-label={strings.zone.clearLayoutAria}
                  sx={{ width: 22, height: 22, p: 0, color: tokens.colorTextSecondary, flexShrink: 0, '&:hover': { bgcolor: tokens.colorPrimaryBg, color: tokens.colorPrimary } }}
                >
                  <DeleteSweepOutlined sx={{ fontSize: 15 }} />
                </IconButton>
              </Tooltip>
            )}
          </Box>
        ))}
      </Box>

      <Box sx={{ px: 1.25, height: 30, display: 'flex', alignItems: 'center', gap: 0.25, borderTop: `1px solid ${tokens.colorBorderLight}`, borderBottom: `1px solid ${tokens.colorBorderLight}`, overflow: 'hidden' }}>
        {toolbarButton(strings.zone.tableButton, <TableChartOutlined sx={{ fontSize: 15 }} />, onInsertTable, { contained: canInsert && !insertBlocked, disabled: !canInsert || insertBlocked, disabledReason: insertDisabledReason || (!canInsert ? strings.zone.addMeasureHint : undefined) })}
        {toolbarButton(strings.zone.chartButton, <InsertChartOutlined sx={{ fontSize: 15 }} />, onInsertChart, { disabled: !canInsert || !onInsertChart || insertBlocked, disabledReason: insertDisabledReason || (!canInsert ? strings.zone.addMeasureHint : undefined) })}
        {toolbarButton(strings.zone.pivotButton, <PivotTableChartOutlined sx={{ fontSize: 15 }} />, onInsertLocalPivot, { disabled: !canInsert || !onInsertLocalPivot || insertBlocked, disabledReason: insertDisabledReason || (!canInsert ? strings.zone.addMeasureHint : undefined) })}
        {toolbarButton(strings.zone.templates, <GridViewOutlined sx={{ fontSize: 15 }} />, onOpenTemplates)}
        <Box sx={{ width: '1px', height: 18, flexShrink: 0, bgcolor: tokens.colorBorderLight, mx: 0.25 }} />
        {onOpenCubeWizard && toolbarButton(strings.reportBuilder.cubeButton, <FunctionsOutlined sx={{ fontSize: 15 }} />, onOpenCubeWizard)}
        {onOpenConnectionWizard && toolbarButton(strings.reportBuilder.connectButton, <AccountTreeOutlined sx={{ fontSize: 15 }} />, onOpenConnectionWizard)}
        {toolbarButton(strings.reportBuilder.traceButton, <ManageSearchOutlined sx={{ fontSize: 15 }} />, onOpenTrace, { disabled: !hasLastQuery })}
        {onRefreshValues && (
          <Tooltip title={strings.reportBuilder.refreshValues}>
            <span>
              <IconButton
                size="small"
                aria-label={strings.reportBuilder.refreshValues}
                title={strings.reportBuilder.refreshValues}
                onClick={(event) => setRefreshMenuAnchor(event.currentTarget)}
                sx={{
                  width: 26, height: 24, borderRadius: 0.5, color: tokens.colorTextSecondary,
                  border: `1px solid ${tokens.colorBorder}`,
                  '&:hover': { bgcolor: tokens.colorPrimaryBg, color: tokens.colorPrimary },
                  '&.Mui-disabled': { color: '#b0b0b0', borderColor: tokens.colorBorderLight },
                }}
              >
                <RefreshOutlined sx={{ fontSize: 15 }} />
              </IconButton>
            </span>
          </Tooltip>
        )}
        <Menu
          anchorEl={refreshMenuAnchor}
          open={Boolean(refreshMenuAnchor)}
          onClose={() => setRefreshMenuAnchor(null)}
          MenuListProps={{ dense: true }}
        >
          <MenuItem sx={{ fontSize: 11 }} onClick={() => { setRefreshMenuAnchor(null); onRefreshValues?.(); }}>
            {strings.reportBuilder.refreshValues}
          </MenuItem>
          {onRefreshSheetData && (
            <MenuItem sx={{ fontSize: 11 }} disabled={refreshSheetDataLoading} onClick={() => { setRefreshMenuAnchor(null); onRefreshSheetData(); }}>
              {strings.tableRefresh.refreshSheetData}
            </MenuItem>
          )}
        </Menu>
        {onInsertModeChange && (
          <Tooltip title={`${insertMode === 'live' ? strings.insertMode.liveDescription : strings.insertMode.staticDescription}`}>
            <Box component="label" sx={{ ml: 'auto', display: 'flex', alignItems: 'center', gap: 0.25, whiteSpace: 'nowrap', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={insertMode === 'live'}
                onChange={(event) => onInsertModeChange(event.target.checked ? 'live' : 'static')}
                aria-label={strings.insertMode.live}
                style={{ width: 13, height: 13, margin: 0, accentColor: tokens.colorPrimary }}
              />
              <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>{strings.insertMode.live}</Typography>
            </Box>
          </Tooltip>
        )}
      </Box>

      {compatibilityWarning && (
        <Alert severity="warning" sx={{ mx: 1.25, my: 0.5, py: 0.375, borderRadius: 0.5, bgcolor: tokens.colorRedBg, color: tokens.colorRed, '& .MuiAlert-icon': { color: tokens.colorRed, py: 0.125 }, '& .MuiAlert-message': { minWidth: 0, py: 0 } }}>
          <Typography sx={{ fontSize: 11, fontWeight: 700, lineHeight: 1.25, color: tokens.colorRed }}>
            {compatibilityWarning.title}
          </Typography>
          {compatibilityWarning.messages.slice(0, 3).map(message => (
            <Typography key={message} sx={{ fontSize: 10.5, lineHeight: 1.3, color: tokens.colorRed }}>
              {message}
            </Typography>
          ))}
          {compatibilityWarning.compatibleDimensionNames && compatibilityWarning.compatibleDimensionNames.length > 0 && (
            <Typography sx={{ fontSize: 10.5, lineHeight: 1.3, color: tokens.colorRed }}>
              {strings.zone.compatibleDimensions} {compatibilityWarning.compatibleDimensionNames.join(', ')}
            </Typography>
          )}
        </Alert>
      )}

      <Dialog open={Boolean(editingFilter)} onClose={() => closeFilterDialog(true)} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 300, borderRadius: 2 } }}>
        <DialogTitle sx={{ fontSize: 14, fontWeight: 700, pb: 0 }}>
          {strings.filter.dialogTitlePrefix} {editingFilter?.name}
        </DialogTitle>
        <DialogContent sx={{ p: 2 }}>
          <FormControl fullWidth size="small" sx={{ mb: 2 }}>
            <InputLabel>{strings.filter.operatorLabel}</InputLabel>
            <Select value={editOperator} label={strings.filter.operatorLabel} onChange={e => { setEditOperator(e.target.value); setEditError(null); setEditNotice(null); }}>
              <MenuItem value="equals">{strings.filter.equals}</MenuItem>
              <MenuItem value="notEquals">{strings.filter.notEquals}</MenuItem>
              <MenuItem value="contains">{strings.filter.contains}</MenuItem>
              <MenuItem value="notContains">{strings.filter.notContains}</MenuItem>
              <MenuItem value="gt">{strings.filter.greaterThan}</MenuItem>
              <MenuItem value="lt">{strings.filter.lessThan}</MenuItem>
              <MenuItem value="inDateRange">{strings.filter.dateRange}</MenuItem>
              <MenuItem value="set">{strings.filter.inSet}</MenuItem>
            </Select>
          </FormControl>
          <TextField
            fullWidth
            size="small"
            label={isScalarOperator ? templates.filter.valueLabel(scalarValueKind) : strings.filter.valuesComma}
            value={editValues}
            onChange={e => { setEditValues(e.target.value); setEditError(null); setEditNotice(null); }}
            error={Boolean(editError)}
            helperText={
              editError
                ? editError
                : isScalarOperator
                  ? templates.filter.enterSingle(scalarValueKind)
                  : editOperator === 'inDateRange'
                    ? strings.filter.enterTwoDates
                    : strings.filter.enterValuesSeparated
            }
          />
          {editNotice && (
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mt: 1 }}>
              {editNotice}
            </Typography>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 2, pb: 1.5 }}>
          <Button size="small" onClick={() => closeFilterDialog(true)} sx={{ textTransform: 'none' }}>{strings.filter.cancel}</Button>
          <Button
            size="small"
            variant="contained"
            onClick={editNotice ? () => closeFilterDialog(false) : handleSaveFilter}
            sx={{ textTransform: 'none' }}
          >
            {editNotice ? strings.filter.done : strings.filter.apply}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
