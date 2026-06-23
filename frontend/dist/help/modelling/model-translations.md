---
title: "Model Translations (i18n)"
audience: modeller
area: modelling
updated: 2026-05-24
---

## What this covers

Multi-language model metadata lets you provide translated display names for dimensions, measures, hierarchies, and other model entities. When a user selects a locale in the Explorer, all entity labels switch to that language. This page explains the translations API, the locale selector, and how translations are resolved at query time.

---

## How it works

Translations are stored in the `entity_translations` table, keyed by **(model_id, entity_type, entity_id, field_name, locale)**. Each row maps a specific field on a specific entity to a translated string in a specific locale.

When a user sets their display locale via the **Locale selector** in the Explorer, the frontend fetches all translations for the active model and locale. Entity labels throughout the UI are resolved using a simple fallback: if a translation exists for the current locale, show it; otherwise, show the original name.

---

## Setting the display locale

1. Open the **Workspace Explorer** or the **Model Builder**.
2. Click the **Locale selector** dropdown (typically near the user avatar or toolbar).
3. Choose a locale (e.g., `fr`, `de`, `ja`, `ar`).
4. All entity labels in the current view update immediately.

The selected locale is stored in the browser session (Zustand store). It persists across page navigations within the same session but resets on logout.

---

## Managing translations

Translations are managed via the REST API. There is no GUI editor yet; use the API directly or build an integration.

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

### Delete a translation

```
DELETE /api/v1/projects/{project_id}/models/{model_id}/translations/{id}
```

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

---

## Related

- [Define Dimensions](define-dimensions.md)
- [Define Measures](define-measures.md)
- [Model Templates](model-templates.md)

---

<- [Model Templates](model-templates.md) | [Home](../index.md) | [Model Details ->](model-details.md)
