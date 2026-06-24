import { useState, useCallback, useEffect, useMemo } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  Button, TextField, Select, MenuItem, Typography, Box,
  FormControl, InputLabel, ThemeProvider, CircularProgress,
} from '@mui/material';
import { CheckCircle, ErrorOutline } from '@mui/icons-material';
import { tokens, theme } from '../../theme';
import { generateCubeValue, measureMemberRef, dimensionMemberRef } from '../../utils/excelFormulas';
import { discoverMembers } from '../../api/queryRouter';
import { useExcelConnections } from '../../hooks/useExcelConnections';
import { useFieldCompatibility } from '../../hooks/useModel';
import { dimensionCompatibilityById } from '../../utils/fieldCompatibility';
import type { Measure, Dimension } from '../../types/tessallite';

interface CubeFormulaWizardProps {
  open: boolean;
  onClose: () => void;
  measures: Measure[];
  dimensions: Dimension[];
  projectId: string;
  modelId: string;
  personaId?: string;
  connectionName: string;
  onInsertFormula: (formula: string, targetCell: string) => void;
}

export default function CubeFormulaWizard({
  open, onClose, measures, dimensions, projectId, modelId, personaId, connectionName, onInsertFormula,
}: CubeFormulaWizardProps) {
  const [step, setStep] = useState(0);
  const [selectedMeasure, setSelectedMeasure] = useState('');
  const [selectedDimension, setSelectedDimension] = useState('');
  const [selectedMember, setSelectedMember] = useState('');
  const [members, setMembers] = useState<{ name: string; key: string }[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);
  const [targetCell, setTargetCell] = useState('A1');
  const [validating, setValidating] = useState(false);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [validated, setValidated] = useState(false);

  const compatibilityDimensionIds = useMemo(() => dimensions.map(d => d.id), [dimensions]);
  const fieldCompatibility = useFieldCompatibility(
    projectId,
    modelId,
    personaId,
    selectedMeasure ? [selectedMeasure] : [],
    compatibilityDimensionIds,
  );
  const dimensionCompatibility = useMemo(
    () => dimensionCompatibilityById(fieldCompatibility.data, selectedMeasure ? [selectedMeasure] : [], dimensions),
    [fieldCompatibility.data, selectedMeasure, dimensions],
  );
  const compatibleDimensions = useMemo(
    () => selectedMeasure && fieldCompatibility.data
      ? dimensions.filter(d => !dimensionCompatibility[d.id]?.disabled)
      : dimensions,
    [selectedMeasure, fieldCompatibility.data, dimensions, dimensionCompatibility],
  );

  // F-025-18: connection presence cannot be detected via Office.js, so the
  // wizard no longer claims "No connection detected" (a permanent false
  // negative). It shows a neutral reminder instead.
  const { refreshConnections } = useExcelConnections();

  const handleNext = useCallback(() => {
    if (step === 0) {
      if (!selectedMeasure) return;
      setStep(1);
    } else {
      setStep(2);
    }
  }, [step, selectedMeasure]);

  const handleBack = useCallback(() => setStep(s => Math.max(s - 1, 0)), []);

  const handleDimensionChange = useCallback(async (dimId: string) => {
    if (dimId && dimensionCompatibility[dimId]?.disabled) return;
    setSelectedDimension(dimId);
    setSelectedMember('');
    setMembers([]);
    if (!dimId) return;
    setMembersLoading(true);
    try {
      const dim = dimensions.find(d => d.id === dimId);
      const result = await discoverMembers(modelId, dim?.name ?? dimId, personaId);
      setMembers(result.members || []);
    } catch {
      setMembers([]);
    } finally {
      setMembersLoading(false);
    }
  }, [dimensions, modelId, personaId, dimensionCompatibility]);

  const handleInsert = useCallback(() => {
    const measure = measures.find(m => m.id === selectedMeasure);
    if (!measure) return;

    // M-1: emit TECHNICAL identifiers, not display names. The gateway's MDX
    // translator resolves measures/dimensions by technical name only
    // (MEASURE_UNIQUE_NAME is `[Measures].[<technical name>]`), so a formula
    // built from display names produces an empty/`#N/A` cell on refresh for any
    // model whose display names differ from technical names (the norm — seed
    // "Base Amount" vs `base_amount`). Display names stay in the wizard UI.
    // F-025-09: each bracket segment is escaped via the shared helpers so a
    // name/key containing ']' cannot break the formula.
    const measureExpr = measureMemberRef(measure.name);
    const filters: string[] = [];
    if (selectedDimension && selectedMember) {
      const dim = dimensions.find(d => d.id === selectedDimension);
      if (dim) {
        filters.push(dimensionMemberRef(dim.name, selectedMember));
      }
    }

    const formula = generateCubeValue(connectionName, measureExpr, filters);
    onInsertFormula(formula, targetCell);
    onClose();
  }, [selectedMeasure, selectedDimension, selectedMember, measures, dimensions, connectionName, targetCell, onInsertFormula, onClose]);

  const handleClose = useCallback(() => {
    setStep(0);
    setSelectedMeasure('');
    setSelectedDimension('');
    setSelectedMember('');
    setMembers([]);
    setTargetCell('A1');
    setValidating(false);
    setValidationErrors([]);
    setValidated(false);
    onClose();
  }, [onClose]);

  useEffect(() => {
    if (open) refreshConnections();
  }, [open, refreshConnections]);

  useEffect(() => {
    if (!selectedDimension) return;
    if (selectedMeasure && dimensionCompatibility[selectedDimension]?.disabled) {
      setSelectedDimension('');
      setSelectedMember('');
      setMembers([]);
    }
  }, [selectedMeasure, selectedDimension, dimensionCompatibility]);

  useEffect(() => {
    if (step !== 2 || !selectedMeasure) return;

    // F-025-02: validate the SEMANTIC INTENT (measure + optional member), not
    // the generated Excel formula text. The previous implementation POSTed the
    // `=CUBEVALUE(...)` string to /validate, which parses raw_query as SQL/DAX —
    // an Excel formula is not SQL, so it ALWAYS returned ok:false and the Insert
    // button stayed permanently disabled. The measure and member are already
    // resolved client-side (the member list comes from /discover/members, which
    // is the model's real, persona-scoped member set), so no formula-text parse
    // is needed. A valid selection enables Insert; an invalid one shows the real
    // reason.
    setValidating(true);
    const errors: string[] = [];

    const measure = measures.find(m => m.id === selectedMeasure);
    if (!measure) {
      errors.push('Selected measure not found in model');
    }
    if (selectedDimension) {
      const dim = dimensions.find(d => d.id === selectedDimension);
      if (!dim) {
        errors.push('Selected dimension not found in model');
      } else if (!selectedMember) {
        errors.push('Select a member for the dimension filter, or remove the filter');
      } else if (!members.some(mem => mem.key === selectedMember)) {
        errors.push('Selected member is no longer available for this dimension');
      }
    }

    setValidationErrors(errors);
    setValidated(errors.length === 0);
    setValidating(false);
  }, [step, selectedMeasure, selectedDimension, selectedMember, measures, dimensions, members]);

  const selectedMeasureObj = measures.find(m => m.id === selectedMeasure);
  const selectedDimensionObj = dimensions.find(d => d.id === selectedDimension);

  // M-1: preview must show the same technical-name formula that Insert emits,
  // so what the analyst previews is exactly what refreshes successfully.
  const previewFormula = selectedMeasureObj
    ? generateCubeValue(
        connectionName,
        measureMemberRef(selectedMeasureObj.name),
        selectedDimensionObj && selectedMember
          ? [dimensionMemberRef(selectedDimensionObj.name, selectedMember)]
          : [],
      )
    : '';

  return (
    <ThemeProvider theme={theme}>
    <Dialog open={open} onClose={handleClose} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 340, borderRadius: 2 } }}>
      <DialogTitle sx={{ fontSize: 14, fontWeight: 700, pb: 0 }}>
        Cube Function Wizard
        <Box component="span" sx={{ fontSize: 11, color: tokens.colorTextSecondary, ml: 1 }}>
          Step {step + 1} of 3
        </Box>
      </DialogTitle>

      <DialogContent sx={{ p: 2 }}>
        {step === 0 && (
          <FormControl fullWidth size="small">
            <InputLabel>Measure</InputLabel>
            <Select
              value={selectedMeasure}
              label="Measure"
              onChange={e => setSelectedMeasure(e.target.value)}
            >
              {measures.map(m => (
                <MenuItem key={m.id} value={m.id}>
                  {m.display_name}
                  <Typography component="span" sx={{ fontSize: 10, color: tokens.colorTextSecondary, ml: 1 }}>
                    ({m.default_agg})
                  </Typography>
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        )}

        {step === 1 && (
          <Box>
            <FormControl fullWidth size="small" sx={{ mb: 1.5 }}>
              <InputLabel>Filter (optional)</InputLabel>
              <Select
                value={selectedDimension}
                label="Filter (optional)"
                onChange={e => handleDimensionChange(e.target.value)}
              >
                <MenuItem value="">None</MenuItem>
                {compatibleDimensions.map(d => (
                  <MenuItem key={d.id} value={d.id}>{d.display_name}</MenuItem>
                ))}
              </Select>
            </FormControl>

            {selectedMeasure && fieldCompatibility.data && compatibleDimensions.length === 0 && (
              <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mb: 1 }}>
                No compatible dimensions are available for this measure.
              </Typography>
            )}

            {selectedDimension && membersLoading && (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 1 }}>
                <CircularProgress size={16} sx={{ color: tokens.colorPrimary }} />
              </Box>
            )}

            {selectedDimension && !membersLoading && members.length > 0 && (
              <FormControl fullWidth size="small">
                <InputLabel>Member</InputLabel>
                <Select
                  value={selectedMember}
                  label="Member"
                  onChange={e => setSelectedMember(e.target.value)}
                >
                  {members.map(mem => (
                    <MenuItem key={mem.key} value={mem.key}>{mem.name}</MenuItem>
                  ))}
                </Select>
              </FormControl>
            )}

            {selectedDimension && !membersLoading && members.length === 0 && (
              <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
                No members found for this dimension.
              </Typography>
            )}
          </Box>
        )}

        {step === 2 && (
          <Box>
            <Box sx={{ bgcolor: tokens.colorSubtleFill, p: 1, borderRadius: 1, mb: 1.5 }}>
              <Typography sx={{ fontSize: 12, fontFamily: tokens.fontMono, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                {previewFormula}
              </Typography>
            </Box>
            <TextField
              fullWidth
              size="small"
              label="Target cell"
              value={targetCell}
              onChange={e => setTargetCell(e.target.value)}
              sx={{ mb: 1 }}
            />
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mb: 0.5 }}>
              CUBE formulas resolve against a workbook connection named "{connectionName}".
              If you have not set one up yet, open Report Builder and use "Live connection"
              to create it. The formula is inserted regardless; it will show #N/A until the
              connection exists.
            </Typography>
            {validating && (
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                <CircularProgress size={12} sx={{ color: tokens.colorPrimary }} />
                <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
                  Validating...
                </Typography>
              </Box>
            )}
            {!validating && validationErrors.length > 0 && (
              <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.5 }}>
                <ErrorOutline sx={{ fontSize: 14, color: tokens.colorRed, mt: 0.25 }} />
                <Box>
                  {validationErrors.map((err, i) => (
                    <Typography key={i} sx={{ fontSize: 11, color: tokens.colorRed }}>
                      {err}
                    </Typography>
                  ))}
                </Box>
              </Box>
            )}
            {!validating && validated && validationErrors.length === 0 && (
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                <CheckCircle sx={{ fontSize: 14, color: tokens.colorPrimary }} />
                <Typography sx={{ fontSize: 11, color: tokens.colorPrimary }}>
                  Ready to insert at {targetCell}
                </Typography>
              </Box>
            )}
          </Box>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 2, pb: 1.5 }}>
        <Button size="small" onClick={handleClose} sx={{ textTransform: 'none' }}>Cancel</Button>
        <Box sx={{ flex: 1 }} />
        {step > 0 && (
          <Button size="small" variant="outlined" onClick={handleBack} sx={{ textTransform: 'none' }}>Back</Button>
        )}
        {step < 2 ? (
          <Button
            size="small"
            variant="contained"
            onClick={handleNext}
            disabled={step === 0 && !selectedMeasure}
            sx={{ textTransform: 'none' }}
          >
            Next
          </Button>
        ) : (
          <Button
            size="small"
            variant="contained"
            onClick={handleInsert}
            disabled={!selectedMeasure || validating || validationErrors.length > 0}
            sx={{ textTransform: 'none' }}
          >
            Insert Formula
          </Button>
        )}
      </DialogActions>
    </Dialog>
    </ThemeProvider>
  );
}
