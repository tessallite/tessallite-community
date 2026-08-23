import { useState } from 'react';
import { Box, Button, Typography } from '@mui/material';
import { tokens } from '../../theme';
import { strings, templates } from '../../i18n/strings';
import type { RefreshDetailRow } from '../../utils/tableRefresh';

/**
 * Bug-7397 R12-5: the details surface behind the sheet-refresh toast.
 *
 * `refreshTables` computes a per-table REASON for every skip and every warning,
 * and the R6-R11 redesign invested heavily in making those reasons honest and
 * actionable ("a concurrent change was detected... refresh again", "the table
 * changed size... refresh again"). None of it was reachable: the toast said
 * "1 skipped (see details)" and no details view existed anywhere in `src/` --
 * `templates.tableRefresh.skippedDetail` had zero call sites. A user could not
 * learn which table was skipped, why, or that a second click would fix a
 * transient skip.
 *
 * Rendered inline under the refresh button rather than as a modal, so the user
 * can read a reason and act on it in the sheet without dismissing anything.
 */
/** Stable id linking the toggle to the region it controls (a11y). */
const DETAILS_REGION_ID = 'tessallite-refresh-details';

export default function RefreshDetailsPanel({ rows }: { rows: RefreshDetailRow[] }) {
  const [open, setOpen] = useState(false);
  if (rows.length === 0) return null;

  const groups: { kind: RefreshDetailRow['kind']; label: string }[] = [
    { kind: 'skipped', label: strings.tableRefresh.detailsSkippedGroup },
    { kind: 'warning', label: strings.tableRefresh.detailsWarningGroup },
  ];

  return (
    <Box sx={{ px: 1.25, pb: 1.25 }}>
      <Button
        size="small"
        variant="text"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        // Only reference the region while it EXISTS -- aria-controls pointing
        // at an absent id is an aria-valid-attr-value violation when collapsed.
        aria-controls={open ? DETAILS_REGION_ID : undefined}
        sx={{ fontSize: 11, minWidth: 0, px: 0.5 }}
      >
        {open ? strings.tableRefresh.detailsHide : strings.tableRefresh.detailsShow}
      </Button>
      {open && (
        <Box
          id={DETAILS_REGION_ID}
          role="region"
          aria-label={strings.tableRefresh.detailsTitle}
          sx={{
            mt: 0.5,
            p: 1,
            borderRadius: 1,
            border: `1px solid ${tokens.colorBorder}`,
            bgcolor: tokens.colorSubtleFill,
          }}
        >
          <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorCharcoal, mb: 0.5 }}>
            {strings.tableRefresh.detailsTitle}
          </Typography>
          {groups.map(group => {
            const groupRows = rows.filter(r => r.kind === group.kind);
            if (groupRows.length === 0) return null;
            return (
              <Box key={group.kind} sx={{ mb: 0.5 }}>
                <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, textTransform: 'uppercase' }}>
                  {group.label}
                </Typography>
                {groupRows.map(row => (
                  <Typography
                    key={`${row.kind}-${row.name}-${row.reason}`}
                    sx={{ fontSize: 11, color: tokens.colorCharcoal, mt: 0.25 }}
                  >
                    {templates.tableRefresh.skippedDetail(row.name, row.reason)}
                  </Typography>
                ))}
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
}
