import { createTheme, keyframes } from '@mui/material/styles';

/**
 * Design tokens following Microsoft Office Add-in design language:
 * - Segoe UI typography
 * - Neutral palette with Excel green accent (#217346) used sparingly
 * - Compact task-pane layout (320-350px)
 * - 4px rhythm, 16-20px outer margins
 * - Monoline icons, subdued branding
 *
 * References:
 * - https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-design-language
 * - https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-color
 * - https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-typography
 * - https://learn.microsoft.com/en-us/office/dev/add-ins/design/add-in-layout
 */

export const pulse = keyframes`
  0% { opacity: 0.4; }
  50% { opacity: 1.0; }
  100% { opacity: 0.4; }
`;

export const prefersReducedMotion = typeof window !== 'undefined'
  ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
  : false;

export const tokens = {
  /* ---- accent ---- */
  colorPrimary: '#217346',        // Excel green
  colorPrimaryDark: '#185a33',
  colorPrimaryBg: 'rgba(33,115,70,0.07)',

  /* ---- neutral ---- */
  colorWhite: '#FFFFFF',
  colorBackground: '#FFFFFF',
  colorSubtleFill: '#F5F5F5',    // light surface / hover
  colorBorderLight: '#E1E1E1',
  colorBorder: '#D1D1D1',

  /* ---- text ---- */
  colorCharcoal: '#242424',       // primary text
  colorTextSecondary: '#616161',  // secondary / caption text

  /* ---- semantic ---- */
  colorRed: '#B33A3A',
  colorRedBg: '#FFEBEE',
  colorGold: '#D4AF37',
  colorGoldDark: '#A67C00',
  colorGoldBg: 'rgba(164,124,0,0.08)',
  colorPurple: '#6B4C8A',
  colorPurpleBg: 'rgba(107,76,138,0.08)',
  colorMuted: '#616161',
  colorMutedBg: '#F5F5F5',

  /* ---- typography ---- */
  fontSans: '"Segoe UI", "Segoe UI Web (West European)", system-ui, -apple-system, sans-serif',
  fontMono: '"Cascadia Code", "Fira Code", monospace',
} as const;

export const theme = createTheme({
  palette: {
    primary: {
      main: tokens.colorPrimary,
      dark: tokens.colorPrimaryDark,
    },
    error: { main: tokens.colorRed },
    text: {
      primary: tokens.colorCharcoal,
      secondary: tokens.colorTextSecondary,
    },
    background: { default: tokens.colorWhite },
    divider: tokens.colorBorderLight,
  },
  typography: {
    fontFamily: tokens.fontSans,
    fontSize: 14,
  },
  shape: { borderRadius: 4 },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        html: {
          height: '100%',
          margin: 0,
          backgroundColor: tokens.colorWhite,
        },
        body: {
          height: '100%',
          margin: 0,
          overflow: 'hidden',
          backgroundColor: tokens.colorWhite,
        },
        '#root': {
          height: '100%',
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          textTransform: 'none',
          fontWeight: 600,
          fontSize: 12,
          borderRadius: 4,
          minHeight: 32,
        },
        containedPrimary: {
          backgroundColor: tokens.colorPrimary,
          boxShadow: 'none',
          '&:hover': { boxShadow: 'none' },
        },
        outlined: {
          borderColor: tokens.colorBorder,
          color: tokens.colorCharcoal,
          '&:hover': { borderColor: tokens.colorTextSecondary, backgroundColor: tokens.colorSubtleFill },
        },
        text: {
          color: tokens.colorCharcoal,
          '&:hover': { backgroundColor: tokens.colorSubtleFill },
        },
        sizeSmall: {
          fontSize: 11,
          padding: '2px 8px',
          minHeight: 28,
        },
      },
    },
    MuiTextField: {
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            fontSize: 13,
            borderRadius: 4,
            '& fieldset': { borderColor: tokens.colorBorderLight },
            '&:hover fieldset': { borderColor: tokens.colorBorder },
          },
          '& .MuiInputLabel-root': {
            fontSize: 12,
            color: tokens.colorTextSecondary,
          },
        },
      },
    },
    MuiSelect: {
      styleOverrides: {
        root: {
          fontSize: 13,
          borderRadius: 4,
          '& .MuiOutlinedInput-notchedOutline': { borderColor: tokens.colorBorderLight },
          '&:hover .MuiOutlinedInput-notchedOutline': { borderColor: tokens.colorBorder },
        },
      },
    },
    MuiMenuItem: {
      styleOverrides: {
        root: { fontSize: 13, minHeight: 32 },
      },
    },
    MuiCheckbox: {
      styleOverrides: {
        root: { padding: 2 },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          borderRadius: 4,
          fontSize: 10,
          height: 20,
        },
        outlined: {
          borderColor: tokens.colorBorderLight,
        },
      },
    },
    MuiTypography: {
      styleOverrides: {
        root: {
          color: tokens.colorCharcoal,
        },
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: {
          borderRadius: 4,
        },
      },
    },
    MuiSkeleton: {
      styleOverrides: {
        root: {
          borderRadius: 4,
          backgroundColor: tokens.colorSubtleFill,
        },
      },
    },
  },
  ...(prefersReducedMotion && {
    transitions: {
      duration: {
        shortest: 0,
        shorter: 0,
        short: 0,
        standard: 0,
        complex: 0,
        enteringScreen: 0,
        leavingScreen: 0,
      },
    },
  }),
});
