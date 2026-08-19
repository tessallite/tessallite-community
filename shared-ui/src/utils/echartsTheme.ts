import * as echarts from "echarts/core";

/**
 * Bug-6555 — resolve each distinct echartsTheme OBJECT to a single stable
 * global registry name.
 *
 * ECharts' theme registry is one process-global name->theme map with no
 * unregister API. That leaves two ways to get it wrong:
 *   (a) reuse one shared name (e.g. "tessallite") for different theme objects
 *       -> the last render wins page-wide and every other instance silently
 *       renders the wrong palette; or
 *   (b) mint a fresh per-render / per-instance name -> unbounded registry
 *       growth, because entries are never removed.
 *
 * A module-level WeakMap keyed on the theme object gives each DISTINCT theme
 * exactly one name: identical theme objects (the common case — every host
 * passes one stable singleton theme to all its charts) reuse a single entry
 * (bounded, no leak), while genuinely different theme objects get isolated
 * names (no collision). The WeakMap lets a theme object be GC'd once no host
 * references it. `registerTheme` is only called on a miss, so re-resolving the
 * same object is a no-op.
 *
 * Shared by ChartBlock and VisualArtifactBlock so both chart renderers stay
 * consistent and neither leaks nor collides.
 */
const themeNameRegistry = new WeakMap<object, string>();
let themeNameCounter = 0;

export function resolveEchartsThemeName(
  theme: Record<string, unknown> | undefined,
): string | undefined {
  if (!theme) return undefined;
  let name = themeNameRegistry.get(theme);
  if (!name) {
    name = `tessallite-${themeNameCounter++}`;
    themeNameRegistry.set(theme, name);
    echarts.registerTheme(name, theme);
  }
  return name;
}
