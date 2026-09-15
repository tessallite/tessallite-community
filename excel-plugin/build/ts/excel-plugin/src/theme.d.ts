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
export declare const pulse: {
    name: string;
    styles: string;
    anim: 1;
    toString: () => string;
} & string;
export declare const prefersReducedMotion: boolean;
export declare const tokens: {
    readonly colorPrimary: "#217346";
    readonly colorPrimaryDark: "#185a33";
    readonly colorPrimaryBg: "rgba(33,115,70,0.07)";
    readonly colorWhite: "#FFFFFF";
    readonly colorBackground: "#FFFFFF";
    readonly colorSubtleFill: "#F5F5F5";
    readonly colorBorderLight: "#E1E1E1";
    readonly colorBorder: "#D1D1D1";
    readonly colorCharcoal: "#242424";
    readonly colorTextSecondary: "#616161";
    readonly colorRed: "#B33A3A";
    readonly colorRedBg: "#FFEBEE";
    readonly colorGold: "#D4AF37";
    readonly colorGoldDark: "#A67C00";
    readonly colorGoldBg: "rgba(164,124,0,0.08)";
    readonly colorPurple: "#6B4C8A";
    readonly colorPurpleBg: "rgba(107,76,138,0.08)";
    readonly colorMuted: "#616161";
    readonly colorMutedBg: "#F5F5F5";
    readonly fontSans: "\"Segoe UI\", \"Segoe UI Web (West European)\", system-ui, -apple-system, sans-serif";
    readonly fontMono: "\"Cascadia Code\", \"Fira Code\", monospace";
};
export declare const theme: import("@mui/material").Theme;
