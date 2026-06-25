/**
 * Proportional hero-digit sizing (Bug-5345).
 *
 * A KPI's primary value can be a short integer ("82") or a long formatted
 * string ("$1,234,567"). A single fixed font size makes long values overflow
 * the card or makes short values look undersized next to the visual element.
 *
 * `fitFontSize` returns a font size (in rem) that shrinks as the rendered text
 * grows past a comfortable character budget, clamped to a readable floor. It is
 * deterministic (no DOM measurement) so it is safe in render and in tests.
 *
 * @param text        the value string to display (null/empty falls back to base)
 * @param baseRem     the font size used when the text fits the budget
 * @param budgetChars character count that still renders at the base size
 * @param minRem      smallest font size the value is allowed to shrink to
 */
export function fitFontSize(
  text: string | null | undefined,
  baseRem: number,
  budgetChars = 5,
  minRem = 1.15,
): number {
  if (!text) return baseRem;
  const len = text.length;
  if (len <= budgetChars) return baseRem;
  // Linear shrink: each character beyond the budget trims a slice of the base
  // size, never falling below the floor.
  const overflow = len - budgetChars;
  const shrunk = baseRem - overflow * (baseRem * 0.085);
  return Math.max(minRem, Number(shrunk.toFixed(3)));
}
