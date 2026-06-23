import { useState, useEffect, useCallback } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  Button, Typography, Box, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, Paper, IconButton,
} from '@mui/material';
import { Close, ContentCopy, DeleteOutline, Science } from '@mui/icons-material';
import { tokens } from '../../theme';
import {
  getDiagnosticsReport, copyDiagnostics,
  clearDiagnostics,
} from '../../utils/diagnostics';
import { runCompatibilitySpike, type CompatibilityMatrix } from '../../utils/officeSpike';

interface DiagnosticsPanelProps {
  open: boolean;
  onClose: () => void;
}

export default function DiagnosticsPanel({
  open,
  onClose,
}: DiagnosticsPanelProps) {
  const [report, setReport] = useState('');
  const [copied, setCopied] = useState(false);
  const [spikeResult, setSpikeResult] = useState<CompatibilityMatrix | null>(null);
  const [spikeRunning, setSpikeRunning] = useState(false);
  // F-025-14: the spike now runs against a throwaway hidden worksheet that is
  // deleted afterwards, but it still mutates the workbook (briefly) and reads
  // the active selection, so it stays behind an explicit confirmation.
  const [spikeConfirmOpen, setSpikeConfirmOpen] = useState(false);

  useEffect(() => {
    if (open) {
      setReport(getDiagnosticsReport());
    }
  }, [open]);

  const handleCopy = useCallback(() => {
    copyDiagnostics();
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, []);

  const handleClear = useCallback(() => {
    clearDiagnostics();
    setReport(getDiagnosticsReport());
  }, []);

  const handleSpike = useCallback(async () => {
    setSpikeConfirmOpen(false);
    setSpikeRunning(true);
    setSpikeResult(null);
    try {
      const result = await runCompatibilitySpike();
      setSpikeResult(result);
    } catch (e) {
      setSpikeResult({
        host: 'unknown', platform: 'unknown',
        insertTable: `failed: ${(e as Error).message}`,
        insertChart: 'skipped', localPivotTable: 'skipped',
        cubeFormulas: 'skipped', createXmlaConnection: 'skipped',
        readSelectedCubeFormula: 'skipped', readActiveCell: 'skipped',
        getActiveCellAddress: 'skipped', detectWorkbookConnections: 'skipped',
        notes: 'Spike runner failed',
      });
    } finally {
      setSpikeRunning(false);
    }
  }, []);

  const pluginVersion = '0.1.0';
  const platform = typeof Office !== 'undefined' && Office.context?.platform
    ? Office.context.platform
    : 'Unknown';

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontSize: 14, fontWeight: 700, display: 'flex', alignItems: 'center', pb: 1 }}>
        Diagnostics
        <IconButton size="small" onClick={onClose} aria-label="Close diagnostics" sx={{ ml: 'auto' }}>
          <Close fontSize="small" />
        </IconButton>
      </DialogTitle>

      <DialogContent sx={{ p: 2 }}>
        <Box sx={{ mb: 2 }}>
          <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary, mb: 0.5 }}>
            Environment
          </Typography>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
            <Box sx={{ px: 1.5, py: 0.5, bgcolor: tokens.colorSubtleFill, borderRadius: 1 }}>
              <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>Version</Typography>
              <Typography sx={{ fontSize: 12, fontWeight: 600 }}>{pluginVersion}</Typography>
            </Box>
            <Box sx={{ px: 1.5, py: 0.5, bgcolor: tokens.colorSubtleFill, borderRadius: 1 }}>
              <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>Excel Host</Typography>
              <Typography sx={{ fontSize: 12, fontWeight: 600 }}>{platform}</Typography>
            </Box>
          </Box>
        </Box>

        <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary, mb: 0.5 }}>
          Event Log
        </Typography>

        <TableContainer
          component={Paper}
          variant="outlined"
          sx={{ maxHeight: 320, '& .MuiTableCell-root': { px: 1, py: 0.5, fontSize: 11 } }}
        >
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell sx={{ whiteSpace: 'nowrap', fontWeight: 600 }}>Time</TableCell>
                <TableCell sx={{ whiteSpace: 'nowrap', fontWeight: 600 }}>Type</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>Detail</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {report.split('\n').slice(4, -2).map((line, i) => {
                const parts = line.match(/^\[(.+?)\]\s+(\w+)(.*)$/);
                if (!parts) return null;
                return (
                  <TableRow key={i}>
                    <TableCell sx={{ whiteSpace: 'nowrap', color: tokens.colorTextSecondary }}>
                      {parts[1].slice(11, 19)}
                    </TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>
                      <Box
                        sx={{
                          display: 'inline-block',
                          px: 0.75,
                          py: 0.25,
                          borderRadius: 0.5,
                          fontSize: 9,
                          fontWeight: 600,
                          bgcolor:
                            parts[2] === 'ERROR' ? tokens.colorRedBg :
                            parts[2] === 'API' ? tokens.colorPrimaryBg :
                            tokens.colorSubtleFill,
                          color:
                            parts[2] === 'ERROR' ? tokens.colorRed :
                            parts[2] === 'API' ? tokens.colorPrimary :
                            tokens.colorTextSecondary,
                        }}
                      >
                        {parts[2]}
                      </Box>
                    </TableCell>
                    <TableCell sx={{ fontSize: 10, wordBreak: 'break-all' }}>
                      {parts[3].trim()}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      </DialogContent>

      <DialogActions sx={{ px: 2, pb: 1.5 }}>
        <Button
          size="small"
          startIcon={<DeleteOutline sx={{ fontSize: 14 }} />}
          onClick={handleClear}
          sx={{ textTransform: 'none' }}
        >
          Clear Log
        </Button>
        <Box sx={{ flex: 1 }} />
        <Button
          size="small"
          variant="outlined"
          startIcon={<Science sx={{ fontSize: 14 }} />}
          onClick={() => setSpikeConfirmOpen(true)}
          disabled={spikeRunning}
          sx={{ textTransform: 'none', mr: 0.5 }}
        >
          {spikeRunning ? 'Running...' : 'Run Spike'}
        </Button>
        <Button
          size="small"
          variant="contained"
          startIcon={<ContentCopy sx={{ fontSize: 14 }} />}
          onClick={handleCopy}
          sx={{ textTransform: 'none' }}
        >
          {copied ? 'Copied' : 'Copy Diagnostics'}
        </Button>
      </DialogActions>

      {spikeResult && (
        <Box sx={{ px: 2, pb: 2 }}>
          <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary, mb: 0.5 }}>
            Compatibility Spike — {spikeResult.host} / {spikeResult.platform}
          </Typography>
          <Box
            sx={{ bgcolor: tokens.colorSubtleFill, p: 1, borderRadius: 1, fontFamily: tokens.fontMono, fontSize: 10, whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}
          >
            {JSON.stringify(spikeResult, null, 2)}
          </Box>
        </Box>
      )}

      <Dialog open={spikeConfirmOpen} onClose={() => setSpikeConfirmOpen(false)} maxWidth="xs">
        <DialogTitle sx={{ fontSize: 14, fontWeight: 700 }}>Run compatibility spike?</DialogTitle>
        <DialogContent sx={{ pt: 0 }}>
          <Typography sx={{ fontSize: 12 }}>
            The spike checks whether this Excel host supports tables, charts and CUBE
            formulas. It writes its test data into a temporary hidden worksheet that
            is deleted immediately afterwards, and reads (but does not change) your
            current cell selection. Your workbook content is not modified.
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 2, pb: 1.5 }}>
          <Button size="small" onClick={() => setSpikeConfirmOpen(false)} sx={{ textTransform: 'none' }}>
            Cancel
          </Button>
          <Button size="small" variant="contained" onClick={handleSpike} sx={{ textTransform: 'none' }}>
            Run Spike
          </Button>
        </DialogActions>
      </Dialog>
    </Dialog>
  );
}
