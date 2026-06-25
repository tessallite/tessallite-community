# Tessallite Frontend i18n Translation Coverage Report

**Generated:** 2026-06-14 16:25
**Base Language:** English (`en/`)
**Structure:** 14 per-domain JSON files per locale (split from the former monolithic `en.json`)
**Total Keys:** 6,103

A key counts as *translated* when it exists in the target locale **and** its value
differs from the English source. Keys that are absent, or copied verbatim from English,
count as *untranslated*. Note that many "untranslated" entries are intentional — machine
event ids, acronyms, and brand names that must stay identical across all languages (see below).

## Executive Summary

| Language | Coverage | Translated | Untranslated | Status |
|----------|---------:|-----------:|-------------:|--------|
| Arabic (العربية) | 96.5% | 5,892 | 211 | Excellent |
| Chinese (中文) | 96.2% | 5,872 | 231 | Excellent |
| Japanese (日本語) | 96.1% | 5,868 | 235 | Excellent |
| Spanish (Español) | 94.7% | 5,780 | 323 | Good |
| Portuguese (Português) | 94.1% | 5,740 | 363 | Good |
| German (Deutsch) | 92.3% | 5,631 | 472 | Good |
| French (Français) | 91.9% | 5,609 | 494 | Good |

## Untranslated Keys — Real Gap vs Intentional

Most untranslated keys are **intentionally** left in English: event identifiers
(e.g. `model.published`), acronyms (`IP`, `UUID`, `SQL`), and brand names
(`Tessallite`, `PostgreSQL`, `BigQuery`, `Power BI`). These have no target-language
form and are correctly held as-is by the localization rules. The *real* translation
gap is the multi-word column below.

| Language | Untranslated | Identifier / brand (intentional) | Real text gap |
|----------|-------------:|---------------------------------:|--------------:|
| Arabic (العربية) | 211 | ~154 | ~57 |
| Chinese (中文) | 231 | ~170 | ~61 |
| Japanese (日本語) | 235 | ~168 | ~67 |
| Spanish (Español) | 323 | ~254 | ~69 |
| Portuguese (Português) | 363 | ~278 | ~85 |
| German (Deutsch) | 472 | ~387 | ~85 |
| French (Français) | 494 | ~412 | ~82 |

## Coverage by Domain File

Average coverage across all 7 target locales, with the weakest locale in parentheses.

| File | Keys | Avg coverage | Weakest locale |
|------|-----:|-------------:|---------------:|
| `builder.json` | 1,147 | 94.4% | 90.1% |
| `panels.json` | 1,116 | 96.5% | 92.7% |
| `kpis.json` | 736 | 97.2% | 94.4% |
| `explorer.json` | 609 | 93.6% | 89.8% |
| `agent.json` | 504 | 93.4% | 91.3% |
| `ui.json` | 389 | 95.7% | 93.8% |
| `settings.json` | 385 | 83.5% | 80.8% |
| `admin.json` | 344 | 96.3% | 94.8% |
| `importExport.json` | 283 | 93.7% | 91.5% |
| `common.json` | 238 | 92.8% | 89.5% |
| `calendar.json` | 125 | 97.4% | 93.6% |
| `glossary.json` | 116 | 98.9% | 96.6% |
| `collibra.json` | 57 | 96.7% | 89.5% |
| `solidatus.json` | 54 | 96.8% | 92.6% |

`settings.json` is the weakest domain across every locale and is the priority for the next pass.

## Top 20 Key Categories

By first dotted segment of the key.

| Rank | Category | Keys | % of Total |
|------|----------|-----:|-----------:|
| 1 | kpis | 344 | 5.6% |
| 2 | agent | 293 | 4.8% |
| 3 | ui | 226 | 3.7% |
| 4 | kpiBusiness | 216 | 3.5% |
| 5 | modelHealth | 161 | 2.6% |
| 6 | pages | 145 | 2.4% |
| 7 | namedSets | 112 | 1.8% |
| 8 | glossary | 105 | 1.7% |
| 9 | hierarchies | 103 | 1.7% |
| 10 | measures | 97 | 1.6% |
| 11 | settings | 96 | 1.6% |
| 12 | importDialog | 92 | 1.5% |
| 13 | pocketTables | 90 | 1.5% |
| 14 | webhooks | 90 | 1.5% |
| 15 | diagnostics | 88 | 1.4% |
| 16 | sources | 87 | 1.4% |
| 17 | tenantAdmin | 83 | 1.4% |
| 18 | pivot | 80 | 1.3% |
| 19 | scheduler | 76 | 1.2% |
| 20 | llm | 74 | 1.2% |

## Recommendations

1. **Close the real-text gap first.** Largest genuine gaps: German (~85), Portuguese (~85) multi-word strings;
   the identifier/brand counts can be ignored.
2. **Prioritize `settings.json`** — lowest coverage in every locale.
3. **Run the filler tool** to close gaps automatically:
   `./translate-i18n.sh --dry-run` to scope, then without `--dry-run` to translate
   missing keys per locale using the per-language `*-localization-rules.md` files.
4. **Re-generate this report** after each translation pass; numbers drift as the
   tool writes locale files.

---

*Snapshot computed from the live locale files; counts shift while the translation tool is running.*
