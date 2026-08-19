"""Entity translations API — CRUD, export/import, bootstrap, coverage for i18n."""
from __future__ import annotations

import csv
import io
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.auth.middleware import CurrentUser, get_current_user
from src.auth.rbac import require_role
from src.api._model_lock import acquire_model_definition_lock
from shared.db.models import (
    Dimension,
    EntityTranslation,
    GlossaryEntry,
    Measure,
    Model,
)
from shared.db.session import get_tenant_db
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["translations"])

SUPPORTED_LOCALES = {"en", "ar", "fr", "es", "de", "pt", "zh", "ja"}
_settings = get_settings()


def resolve_locale(
    accept_language: str | None = Header(None, alias="Accept-Language"),
) -> str:
    """Parse Accept-Language header and return best matching supported locale."""
    if not accept_language:
        return "en"
    candidates: list[tuple[str, float]] = []
    for part in accept_language.split(","):
        part = part.strip()
        if not part:
            continue
        if ";q=" in part:
            tag, q_str = part.split(";q=", 1)
            try:
                q = float(q_str.strip())
            except ValueError:
                q = 0.0
        else:
            tag = part
            q = 1.0
        candidates.append((tag.strip().lower(), q))
    candidates.sort(key=lambda x: x[1], reverse=True)
    for tag, _ in candidates:
        base = tag.split("-")[0]
        if base in SUPPORTED_LOCALES:
            return base
        if tag in SUPPORTED_LOCALES:
            return tag
    return "en"
SUPPORTED_ENTITY_TYPES = {"glossary_entry", "measure", "dimension", "model"}
SUPPORTED_FIELD_NAMES = {
    "dimension": {"display_name", "description"},
    "measure": {"display_name", "description"},
    "glossary_entry": {"term", "definition"},
    "model": {"display_name", "description"},
}


class TranslationCreate(BaseModel):
    entity_type: str
    entity_id: UUID
    field_name: str
    locale: str
    translated_text: str
    source: str = "user"


class TranslationResponse(BaseModel):
    id: UUID
    model_id: UUID
    entity_type: str
    entity_id: UUID
    field_name: str
    locale: str
    translated_text: str
    source: str


class TranslationBulkItem(BaseModel):
    entity_type: str
    entity_id: UUID
    field_name: str
    locale: str
    translated_text: str
    source: str = "user"


class TranslationBulkRequest(BaseModel):
    translations: list[TranslationBulkItem]


async def _verify_model_in_project(db, model_id: UUID, project_id: UUID) -> None:
    model = await db.get(Model, model_id)
    if not model or model.project_id != project_id:
        raise HTTPException(status_code=404, detail="Model not found in project")


_ENTITY_TYPE_MODEL_CLASS = {
    "dimension": Dimension,
    "measure": Measure,
    # "glossary_entry" is NOT here — it needs an extra superseded_by check
    # (see _validate_translation_target's dedicated branch below).
}


async def _validate_translation_target(
    db, model_id: UUID, entity_type: str, entity_id: UUID,
) -> bool:
    """True when *entity_id* refers to a live entity of *entity_type*
    scoped to *model_id*, i.e. a valid target for ``EntityTranslation``.

    Bug-5981 (F-029-02): ``EntityTranslation.entity_id`` is a polymorphic
    soft reference with no database foreign key — create/bulk/import
    previously validated only the ``entity_type``/``locale`` enum strings,
    never that ``entity_id`` actually resolves. An orphan translation
    (typo'd id, id from a different model, id of a since-deleted entity)
    silently inflates translation coverage while never rendering anywhere
    (``useModelTranslations.ts`` matches by exact entity id). Call this
    from every write path before persisting a translation row.
    """
    if entity_type == "model":
        # A model translates its own display_name/description; the
        # convention is entity_id == the model itself.
        return entity_id == model_id
    if entity_type == "glossary_entry":
        # Bug-5981 review round 1 (M3): translation_coverage's numerator
        # and denominator both exclude superseded glossary entries
        # (superseded_by IS NOT NULL) — a superseded entry is being
        # actively replaced and its own text is no longer live. Accepting
        # a translation for one here would create exactly the write/read
        # asymmetry this bug is about: accepted at write time, never
        # counted or rendered.
        row = await db.get(GlossaryEntry, entity_id)
        return (
            row is not None
            and row.model_id == model_id
            and row.superseded_by is None
        )
    model_class = _ENTITY_TYPE_MODEL_CLASS.get(entity_type)
    if model_class is None:
        return False
    row = await db.get(model_class, entity_id)
    return row is not None and row.model_id == model_id


def _validate_translation_field(entity_type: str, field_name: str) -> bool:
    return field_name in SUPPORTED_FIELD_NAMES.get(entity_type, set())


def _coverage_live_entity_filter(dim_ids, meas_ids, glossary_ids):
    """Rows that count in the translation coverage numerator.

    Bug-6418: model-level translations are valid write targets, but the coverage
    denominator is dimension/measure/glossary fields only. Excluding model rows
    here keeps numerator and denominator aligned.
    """
    return (
        ((EntityTranslation.entity_type == "dimension") & EntityTranslation.entity_id.in_(dim_ids))
        | ((EntityTranslation.entity_type == "measure") & EntityTranslation.entity_id.in_(meas_ids))
        | ((EntityTranslation.entity_type == "glossary_entry") & EntityTranslation.entity_id.in_(glossary_ids))
    )


@router.get(
    "/projects/{project_id}/models/{model_id}/translations",
    response_model=list[TranslationResponse],
    dependencies=[require_role("viewer")],
)
async def list_translations(
    project_id: UUID,
    model_id: UUID,
    locale: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[TranslationResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)
        q = select(EntityTranslation).where(EntityTranslation.model_id == model_id)
        if locale:
            q = q.where(EntityTranslation.locale == locale)
        if entity_type:
            q = q.where(EntityTranslation.entity_type == entity_type)
        if entity_id:
            q = q.where(EntityTranslation.entity_id == entity_id)
        result = await db.execute(q)
        rows = list(result.scalars().all())
        return [
            TranslationResponse(
                id=r.id,
                model_id=r.model_id,
                entity_type=r.entity_type,
                entity_id=r.entity_id,
                field_name=r.field_name,
                locale=r.locale,
                translated_text=r.translated_text,
                source=r.source,
            )
            for r in rows
        ]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/projects/{project_id}/models/{model_id}/translations",
    response_model=TranslationResponse,
    status_code=201,
    dependencies=[require_role("modeler")],
)
async def create_translation(
    project_id: UUID,
    model_id: UUID,
    body: TranslationCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> TranslationResponse:
    if body.locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=400, detail=f"Unsupported locale: {body.locale}")
    if body.entity_type not in SUPPORTED_ENTITY_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported entity type: {body.entity_type}")
    if not _validate_translation_field(body.entity_type, body.field_name):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported field for {body.entity_type}: {body.field_name}",
        )

    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)
        # Bug-7982 finding 7 then 3: auth before lock; EntityTranslation is
        # snapshot-owned (truncate-reinserted on revert).
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        # Bug-5981: reject an orphan entity_id before it can ever be
        # persisted — see _validate_translation_target for why.
        if not await _validate_translation_target(db, model_id, body.entity_type, body.entity_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No {body.entity_type} with id {body.entity_id} exists "
                    "on this model; cannot create a translation for it."
                ),
            )
        existing = await db.execute(
            select(EntityTranslation).where(
                EntityTranslation.model_id == model_id,
                EntityTranslation.entity_type == body.entity_type,
                EntityTranslation.entity_id == body.entity_id,
                EntityTranslation.field_name == body.field_name,
                EntityTranslation.locale == body.locale,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.translated_text = body.translated_text
            row.source = body.source
            await db.commit()
            await db.refresh(row)
            return TranslationResponse(
                id=row.id,
                model_id=row.model_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                field_name=row.field_name,
                locale=row.locale,
                translated_text=row.translated_text,
                source=row.source,
            )

        t = EntityTranslation(
            model_id=model_id,
            entity_type=body.entity_type,
            entity_id=body.entity_id,
            field_name=body.field_name,
            locale=body.locale,
            translated_text=body.translated_text,
            source=body.source,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return TranslationResponse(
            id=t.id,
            model_id=t.model_id,
            entity_type=t.entity_type,
            entity_id=t.entity_id,
            field_name=t.field_name,
            locale=t.locale,
            translated_text=t.translated_text,
            source=t.source,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/projects/{project_id}/models/{model_id}/translations/bulk",
    response_model=list[TranslationResponse],
    status_code=201,
    dependencies=[require_role("modeler")],
)
async def bulk_upsert_translations(
    project_id: UUID,
    model_id: UUID,
    body: TranslationBulkRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[TranslationResponse]:
    results: list[TranslationResponse] = []
    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock

        skipped: list[str] = []
        upserted_rows: list[EntityTranslation] = []

        for idx, item in enumerate(body.translations):
            if item.locale not in SUPPORTED_LOCALES:
                skipped.append(f"[{idx}] unsupported locale: {item.locale}")
                continue
            if item.entity_type not in SUPPORTED_ENTITY_TYPES:
                skipped.append(f"[{idx}] unsupported entity type: {item.entity_type}")
                continue
            if not _validate_translation_field(item.entity_type, item.field_name):
                skipped.append(f"[{idx}] unsupported field for {item.entity_type}: {item.field_name}")
                continue
            # Bug-5981: same orphan-reference guard as create_translation.
            if not await _validate_translation_target(db, model_id, item.entity_type, item.entity_id):
                skipped.append(
                    f"[{idx}] no {item.entity_type} with id {item.entity_id} "
                    "exists on this model"
                )
                continue

            existing = await db.execute(
                select(EntityTranslation).where(
                    EntityTranslation.model_id == model_id,
                    EntityTranslation.entity_type == item.entity_type,
                    EntityTranslation.entity_id == item.entity_id,
                    EntityTranslation.field_name == item.field_name,
                    EntityTranslation.locale == item.locale,
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.translated_text = item.translated_text
                row.source = item.source
                upserted_rows.append(row)
            else:
                row = EntityTranslation(
                    model_id=model_id,
                    entity_type=item.entity_type,
                    entity_id=item.entity_id,
                    field_name=item.field_name,
                    locale=item.locale,
                    translated_text=item.translated_text,
                    source=item.source,
                )
                db.add(row)
                upserted_rows.append(row)

        if skipped:
            logger.warning("Bulk upsert skipped %d items: %s", len(skipped), "; ".join(skipped))

        await db.commit()

        for r in upserted_rows:
            await db.refresh(r)
            results.append(
                TranslationResponse(
                    id=r.id,
                    model_id=r.model_id,
                    entity_type=r.entity_type,
                    entity_id=r.entity_id,
                    field_name=r.field_name,
                    locale=r.locale,
                    translated_text=r.translated_text,
                    source=r.source,
                )
            )
        return results
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete(
    "/projects/{project_id}/models/{model_id}/translations/{translation_id}",
    status_code=204,
    dependencies=[require_role("modeler")],
)
async def delete_translation(
    project_id: UUID,
    model_id: UUID,
    translation_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        result = await db.execute(
            delete(EntityTranslation).where(
                EntityTranslation.id == translation_id,
                EntityTranslation.model_id == model_id,
            )
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Translation not found")
        await db.commit()
        return
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/models/{model_id}/translations/export",
    dependencies=[require_role("viewer")],
)
async def export_translations(
    project_id: UUID,
    model_id: UUID,
    locale: str | None = None,
    format: str = "csv",
    current_user: CurrentUser = Depends(get_current_user),
):
    """Export translations as CSV or JSON."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)
        q = select(EntityTranslation).where(EntityTranslation.model_id == model_id)
        if locale:
            q = q.where(EntityTranslation.locale == locale)
        result = await db.execute(q)
        rows = list(result.scalars().all())

        if format == "json":
            data = [
                {
                    "entity_type": r.entity_type,
                    "entity_id": str(r.entity_id),
                    "field_name": r.field_name,
                    "locale": r.locale,
                    "translated_text": r.translated_text,
                    "source": r.source,
                }
                for r in rows
            ]
            import json as _json

            body = _json.dumps(data, indent=2, ensure_ascii=False)
            return StreamingResponse(
                io.BytesIO(body.encode("utf-8")),
                media_type="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="translations_{model_id}.json"'
                },
            )

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["entity_type", "entity_id", "field_name", "locale", "translated_text", "source"]
        )
        for r in rows:
            writer.writerow(
                [r.entity_type, str(r.entity_id), r.field_name, r.locale, r.translated_text, r.source]
            )
        csv_bytes = buf.getvalue().encode("utf-8")
        return StreamingResponse(
            io.BytesIO(csv_bytes),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="translations_{model_id}.csv"'
            },
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

class TranslationImportResponse(BaseModel):
    imported: int
    skipped: int
    errors: list[str]


@router.post(
    "/projects/{project_id}/models/{model_id}/translations/import",
    response_model=TranslationImportResponse,
    dependencies=[require_role("modeler")],
)
async def import_translations(
    project_id: UUID,
    model_id: UUID,
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
) -> TranslationImportResponse:
    """Import translations from a CSV or JSON file."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock

        raw = await file.read()
        text = raw.decode("utf-8-sig")

        items: list[dict] = []
        errors: list[str] = []

        if file.filename and file.filename.endswith(".json"):
            import json as _json

            try:
                parsed = _json.loads(text)
            except _json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
            # Bug-7664: validate the parsed JSON is a list of dicts with the
            # required keys. Before this fix, a top-level object (e.g.
            # ``{"translations": [...]}``), a non-dict item, or a missing key
            # raised an unhandled 500 (AttributeError/KeyError) instead of
            # using the endpoint's own ``errors`` report.
            if not isinstance(parsed, list):
                raise HTTPException(
                    status_code=400,
                    detail="JSON must be a top-level array of translation objects",
                )
            _json_required = {"entity_type", "entity_id", "field_name", "locale", "translated_text"}
            for idx, entry in enumerate(parsed):
                if not isinstance(entry, dict):
                    errors.append(f"[{idx}] item is not an object")
                    continue
                missing = _json_required - entry.keys()
                if missing:
                    errors.append(f"[{idx}] missing keys: {sorted(missing)}")
                    continue
                items.append(entry)
        else:
            reader = csv.DictReader(io.StringIO(text))
            for idx, row in enumerate(reader):
                required = {"entity_type", "entity_id", "field_name", "locale", "translated_text"}
                if not required.issubset(row.keys()):
                    errors.append(f"Row {idx}: missing columns {required - row.keys()}")
                    continue
                items.append(row)

        imported = 0
        skipped = 0
        for idx, item in enumerate(items):
            loc = item.get("locale", "")
            etype = item.get("entity_type", "")
            if loc not in SUPPORTED_LOCALES:
                errors.append(f"[{idx}] unsupported locale: {loc}")
                skipped += 1
                continue
            if etype not in SUPPORTED_ENTITY_TYPES:
                errors.append(f"[{idx}] unsupported entity type: {etype}")
                skipped += 1
                continue
            if not _validate_translation_field(etype, item["field_name"]):
                errors.append(f"[{idx}] unsupported field for {etype}: {item['field_name']}")
                skipped += 1
                continue
            try:
                eid = UUID(item["entity_id"])
            except (ValueError, KeyError):
                errors.append(f"[{idx}] invalid entity_id")
                skipped += 1
                continue
            # Bug-5981: same orphan-reference guard as create_translation —
            # a file can claim any UUID shape; validate it resolves to a
            # live entity on this model before importing it as "successful".
            if not await _validate_translation_target(db, model_id, etype, eid):
                errors.append(f"[{idx}] no {etype} with id {eid} exists on this model")
                skipped += 1
                continue

            existing = await db.execute(
                select(EntityTranslation).where(
                    EntityTranslation.model_id == model_id,
                    EntityTranslation.entity_type == etype,
                    EntityTranslation.entity_id == eid,
                    EntityTranslation.field_name == item["field_name"],
                    EntityTranslation.locale == loc,
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.translated_text = item["translated_text"]
                row.source = item.get("source", "import")
            else:
                db.add(
                    EntityTranslation(
                        model_id=model_id,
                        entity_type=etype,
                        entity_id=eid,
                        field_name=item["field_name"],
                        locale=loc,
                        translated_text=item["translated_text"],
                        source=item.get("source", "import"),
                    )
                )
            imported += 1

        await db.commit()
        return TranslationImportResponse(imported=imported, skipped=skipped, errors=errors)
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

class LocaleCoverage(BaseModel):
    locale: str
    translated: int
    total: int
    percent: float


class CoverageResponse(BaseModel):
    total_translatable: int
    locales: list[LocaleCoverage]


@router.get(
    "/projects/{project_id}/models/{model_id}/translations/coverage",
    response_model=CoverageResponse,
    dependencies=[require_role("viewer")],
)
async def translation_coverage(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> CoverageResponse:
    """Return translation coverage stats per locale."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)

        dim_ids = (
            await db.execute(select(Dimension.id).where(Dimension.model_id == model_id))
        ).scalars().all()
        meas_ids = (
            await db.execute(select(Measure.id).where(Measure.model_id == model_id))
        ).scalars().all()
        glossary_ids = (
            await db.execute(
                select(GlossaryEntry.id).where(
                    GlossaryEntry.model_id == model_id,
                    GlossaryEntry.superseded_by.is_(None),
                )
            )
        ).scalars().all()

        total_translatable = (len(dim_ids) + len(meas_ids) + len(glossary_ids)) * 2

        # Bug-5981: count only translations that resolve to a live entity —
        # an orphan row (deleted entity, cross-model id, typo'd import id)
        # must not inflate the "translated" numerator. Write paths now
        # reject orphans at creation time (_validate_translation_target),
        # but this defends against any that predate the fix or slip
        # through a path this pass didn't cover.
        locale_stats = await db.execute(
            select(EntityTranslation.locale, func.count())
            .where(
                EntityTranslation.model_id == model_id,
                _coverage_live_entity_filter(dim_ids, meas_ids, glossary_ids),
            )
            .group_by(EntityTranslation.locale)
        )
        locales = []
        for loc, count in locale_stats.all():
            pct = (count / total_translatable * 100) if total_translatable > 0 else 0.0
            locales.append(
                LocaleCoverage(
                    locale=loc,
                    translated=count,
                    total=total_translatable,
                    percent=round(pct, 1),
                )
            )
        locales.sort(key=lambda x: x.percent, reverse=True)
        return CoverageResponse(total_translatable=total_translatable, locales=locales)
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# Bootstrap (LLM-powered translation generation)
# ---------------------------------------------------------------------------

class BootstrapTranslationsRequest(BaseModel):
    target_locale: str


class BootstrapTranslationsResponse(BaseModel):
    proposed: int
    errors: list[str]


@router.post(
    "/projects/{project_id}/models/{model_id}/translations/bootstrap",
    response_model=BootstrapTranslationsResponse,
    dependencies=[require_role("modeler")],
)
async def bootstrap_translations(
    project_id: UUID,
    model_id: UUID,
    body: BootstrapTranslationsRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> BootstrapTranslationsResponse:
    """Use LLM to generate translations for all dimensions, measures, and
    glossary entries that lack a translation in the target locale."""
    if body.target_locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=400, detail=f"Unsupported locale: {body.target_locale}")
    if body.target_locale == "en":
        raise HTTPException(status_code=400, detail="Cannot bootstrap translations for the base locale")

    locale_names = {
        "ar": "Arabic", "fr": "French", "es": "Spanish",
        "de": "German", "pt": "Portuguese", "zh": "Chinese", "ja": "Japanese",
    }
    target_lang = locale_names.get(body.target_locale, body.target_locale)

    async for db in get_tenant_db(current_user.tenant_id):
        await _verify_model_in_project(db, model_id, project_id)

        existing_q = await db.execute(
            select(
                EntityTranslation.entity_type,
                EntityTranslation.entity_id,
                EntityTranslation.field_name,
            ).where(
                EntityTranslation.model_id == model_id,
                EntityTranslation.locale == body.target_locale,
            )
        )
        existing_keys = {(r[0], r[1], r[2]) for r in existing_q.all()}

        items: list[dict] = []

        dims = (await db.execute(
            select(Dimension).where(Dimension.model_id == model_id)
        )).scalars().all()
        for d in dims:
            if ("dimension", d.id, "display_name") not in existing_keys:
                items.append({
                    "entity_type": "dimension",
                    "entity_id": str(d.id),
                    "field_name": "display_name",
                    "source_text": d.display_name or d.name,
                })
            if d.description and ("dimension", d.id, "description") not in existing_keys:
                items.append({
                    "entity_type": "dimension",
                    "entity_id": str(d.id),
                    "field_name": "description",
                    "source_text": d.description,
                })

        measures = (await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )).scalars().all()
        for m in measures:
            if ("measure", m.id, "display_name") not in existing_keys:
                items.append({
                    "entity_type": "measure",
                    "entity_id": str(m.id),
                    "field_name": "display_name",
                    "source_text": m.display_name or m.name,
                })
            if m.description and ("measure", m.id, "description") not in existing_keys:
                items.append({
                    "entity_type": "measure",
                    "entity_id": str(m.id),
                    "field_name": "description",
                    "source_text": m.description,
                })

        glossary_entries = (await db.execute(
            select(GlossaryEntry).where(
                GlossaryEntry.model_id == model_id,
                GlossaryEntry.superseded_by.is_(None),
            )
        )).scalars().all()
        for g in glossary_entries:
            if ("glossary_entry", g.id, "term") not in existing_keys:
                items.append({
                    "entity_type": "glossary_entry",
                    "entity_id": str(g.id),
                    "field_name": "term",
                    "source_text": g.term,
                })
            if g.definition and ("glossary_entry", g.id, "definition") not in existing_keys:
                items.append({
                    "entity_type": "glossary_entry",
                    "entity_id": str(g.id),
                    "field_name": "definition",
                    "source_text": g.definition,
                })

        if not items:
            return BootstrapTranslationsResponse(proposed=0, errors=[])

        errors: list[str] = []
        translations: dict[int, str] = {}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=600.0) as client:
                resp = await client.post(
                    f"{_settings.OPTIMIZER_URL}/api/v1/optimize/translate",
                    json={
                        "target_language": target_lang,
                        "items": [
                            {"index": i, "text": it["source_text"]}
                            for i, it in enumerate(items)
                        ],
                    },
                    params={"tenant_id": current_user.tenant_id},
                    headers={"Authorization": request.headers.get("Authorization", "")},
                )
            if resp.is_success:
                for entry in resp.json().get("translations", []):
                    translations[entry["index"]] = entry["translated_text"]
            else:
                errors.append(f"LLM service returned {resp.status_code}")
        except Exception as exc:
            logger.error("Bootstrap translations LLM call failed: %s", exc, exc_info=True)
            errors.append(f"LLM call failed: {exc}")

        # Bug-7982 finding 3 + finding (d) lesson: EntityTranslation is
        # snapshot-owned, so the WRITE must serialise with deploy/revert — but the
        # lock is acquired HERE, after the (slow, up-to-600s) LLM call above, not
        # at the top of the handler, so it is never held across the external call
        # (the calendar-DDL-under-lock mistake). Auth (_verify_model_in_project)
        # already ran before this point.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        proposed = 0
        for idx, item in enumerate(items):
            text = translations.get(idx)
            if not text:
                continue
            eid = UUID(item["entity_id"])
            # Bug-5838: use INSERT ... ON CONFLICT DO UPDATE so concurrent
            # or retried bootstrap calls upsert atomically instead of racing
            # through a SELECT-then-INSERT window.
            stmt = pg_insert(EntityTranslation).values(
                model_id=model_id,
                entity_type=item["entity_type"],
                entity_id=eid,
                field_name=item["field_name"],
                locale=body.target_locale,
                translated_text=text,
                source="llm",
            ).on_conflict_do_update(
                constraint="uq_entity_translation",
                set_={"translated_text": text, "source": "llm"},
            )
            await db.execute(stmt)
            proposed += 1

        await db.commit()
        return BootstrapTranslationsResponse(proposed=proposed, errors=errors)
    raise HTTPException(status_code=500, detail="DB session exhausted")
