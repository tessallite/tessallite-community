/**
 * Display configuration for number and currency formatting.
 *
 * Bug-908: locale and currency were previously hard-coded inline in
 * measureFormat.ts.  Centralising them here makes them discoverable and
 * easy to replace when per-tenant currency configuration is added.
 *
 * DISPLAY_LOCALE: BCP-47 locale string used for Intl.NumberFormat.
 *   Change this when the platform ships multi-locale number formatting.
 *
 * DISPLAY_CURRENCY: ISO 4217 currency code used for the "currency" measure
 *   format token.  Change this when per-tenant currency is available.
 */

export const DISPLAY_LOCALE = "en-US";
export const DISPLAY_CURRENCY = "USD";
