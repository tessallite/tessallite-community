---
title: "Model Translations (i18n)"
audience: modeller
area: modelling
updated: 2026-08-19
---

## What this covers

Model translations let teams use the same governed model in more than one business language. A finance analyst can see "Revenue" while a French-speaking team sees "Chiffre d'affaires"; both labels point to the same measure, the same rules, and the same deployed model. This page explains what translations are for, how users choose a display language, and how an integration team can maintain translations safely.

**Scope: model labels only.** Translations change the display names of your model objects — dimensions, measures, hierarchies, and levels. They do not translate the Tessallite application itself. The application interface — menus, buttons, panel titles, and dialogs — is shown in English, and there is no feature to translate the interface into other languages. Model translations are a separate, supported capability for the business vocabulary you define; the application chrome is English-only by design.

---

## How it works

Translations are stored in the `entity_translations` table, keyed by **(model_id, entity_type, entity_id, field_name, locale)**. Each row maps one field on one model object to one translated label.

When a user sets their display locale with the **Locale selector**, Tessallite fetches the labels for the active model and locale. If a translation exists, the translated label is shown. If it does not, Tessallite falls back to the original model label. The fallback is deliberate: a partly translated model stays usable while the translation set is being completed.

Translations change labels only. They do not change measure formulas, joins, security rules, aggregate routing, or API names. That is what makes them safe for multilingual reporting: the words can adapt to the audience without changing the numbers.

---

## Setting the display locale

1. Open the **Workspace Explorer** or the **Model Builder**.
2. Click the **Locale selector** dropdown (typically near the user avatar or toolbar).
3. Choose a locale (e.g., `fr`, `de`, `ja`, `ar`).
4. All entity labels in the current view update immediately.

The selected locale is stored in the browser session (Zustand store). It persists across page navigations within the same session but resets on logout.

---

## Managing translations safely

Most teams should treat translations as reference content, not as day-to-day modelling work. The best workflow is to export a list of model objects, have a translator or local business owner review the labels, then load the approved translation set in bulk.

There is no GUI translation editor; model translations are maintained through the REST API. The API is mainly for controlled integration work: importing approved translation files, syncing from a terminology system, or correcting a label during a release. For one-off exploration, keep changes in a staging model first so reviewers can check the wording in context.

### Upsert a single translation

```
PUT /api/v1/projects/{project_id}/models/{model_id}/translations
```

Body:
```json
{
  "entity_type": "dimension",
  "entity_id": "uuid-of-the-dimension",
  "field_name": "display_name",
  "locale": "fr",
  "translated_text": "Catégorie de produit"
}
```

### Bulk upsert

```
POST /api/v1/projects/{project_id}/models/{model_id}/translations/bulk
```

Body: an array of translation objects. The response returns only the rows that were successfully upserted. Skipped items (e.g., missing required fields) are logged server-side with the array index and reason.

### List translations

```
GET /api/v1/projects/{project_id}/models/{model_id}/translations?locale=fr
```

Returns all translations for the specified locale within the model.

### Retire a translation

```
DELETE /api/v1/projects/{project_id}/models/{model_id}/translations/{id}
```

Use delete only as a cleanup operation: for example, when a label was imported for the wrong locale, an entity was renamed and the old wording would now mislead people, or a translation failed review. Do not delete a translation simply because a user wants to see the original English label; changing their locale is safer and reversible.

---

## Supported entity types

| Entity type | Typical field_name | Example |
|---|---|---|
| `dimension` | `display_name` | "Product Category" -> "Catégorie de produit" |
| `measure` | `display_name` | "Revenue" -> "Chiffre d'affaires" |
| `hierarchy` | `display_name` | "Date Hierarchy" -> "Hiérarchie de dates" |
| `level` | `display_name` | "Quarter" -> "Trimestre" |

---

## Security

All translation endpoints verify that the specified model belongs to the specified project. Attempting to manage translations for a model in a different project returns **404 Not Found**.

Translation access should be limited to modellers or controlled automation. A bad translation can create a business misunderstanding even though it cannot change the underlying calculation.

---

## Related

- [Define Dimensions](define-dimensions.md)
- [Define Measures](define-measures.md)
- [Model Templates](model-templates.md)

---

<- [Model Templates](model-templates.md) | [Home](../index.md) | [Model Details ->](model-details.md)
