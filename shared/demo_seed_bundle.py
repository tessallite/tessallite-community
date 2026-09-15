"""Validation for the project bundle used by the demo seed entrypoints.

The demo loaders run before the tenant database is changed.  Keep the checks
here side-effect free so a corrupt or stale reference bundle fails before the
seed transaction starts.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


DEFAULT_EXPORT_FORMAT = "tessallite-project/v1"
_MANIFEST_LINE = re.compile(r"(?P<digest>[0-9a-f]{64})  (?P<name>[^\s]+)")
_ATTACHMENT_TARGETS = {
    "dimension": "dimensions",
    "measure": "measures",
    "column": "columns",
}


class SeedBundleError(ValueError):
    """Raised when a demo seed bundle cannot be trusted for import."""


def _manifest_digest(bundle_path: Path) -> str:
    manifest_path = bundle_path.with_name("MANIFEST.sha256")
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SeedBundleError(
            f"seed bundle manifest is unreadable: {manifest_path}"
        ) from exc

    if len(lines) != 1:
        raise SeedBundleError(
            f"seed bundle manifest must contain exactly one entry for "
            f"{bundle_path.name!r}: {manifest_path}"
        )
    match = _MANIFEST_LINE.fullmatch(lines[0])
    if match is None or match.group("name") != bundle_path.name:
        raise SeedBundleError(
            f"seed bundle manifest must contain '<sha256>  {bundle_path.name}', "
            f"got {lines[0]!r}"
        )
    return match.group("digest")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SeedBundleError(f"seed bundle is unreadable: {path}") from exc
    return digest.hexdigest()


def _model_slug(model: Mapping[str, Any], index: int) -> str:
    metadata = model.get("model")
    if isinstance(metadata, Mapping) and metadata.get("slug"):
        return str(metadata["slug"])
    return f"model[{index}]"


def _ids_for_model(
    model: Mapping[str, Any], plural_type: str, label: str
) -> set[str]:
    values = model.get(plural_type) or []
    if not isinstance(values, list):
        raise SeedBundleError(
            f"{plural_type} for {label!r} must be a list"
        )
    return {
        str(value["id"])
        for value in values
        if isinstance(value, Mapping) and value.get("id") is not None
    }


def validate_glossary_attachments(bundle: Mapping[str, Any]) -> None:
    """Reject glossary attachments that cannot reach a field in their model.

    Attachment rows have no foreign key to dimensions, measures, or columns.
    Importing an attachment with a stale target therefore succeeds but creates
    an unreachable glossary term.  Validate the model-local target set before
    any rehydrator writes occur.
    """
    models = bundle.get("models")
    if not isinstance(models, list):
        raise SeedBundleError("seed bundle 'models' must be a list")

    failures: list[str] = []
    for model_index, model in enumerate(models):
        if not isinstance(model, Mapping):
            failures.append(f"model[{model_index}] is not an object")
            continue
        slug = _model_slug(model, model_index)
        snapshots: list[tuple[str, Mapping[str, Any]]] = [(slug, model)]
        versions = model.get("model_versions") or []
        if not isinstance(versions, list):
            failures.append(f"{slug}: model_versions must be a list")
            versions = []
        for version_index, version in enumerate(versions):
            if not isinstance(version, Mapping):
                failures.append(f"{slug}: model_versions[{version_index}] is not an object")
                continue
            snapshot = version.get("snapshot_json")
            if snapshot is None:
                continue
            if not isinstance(snapshot, Mapping):
                failures.append(
                    f"{slug}/model_versions[{version_index}]: snapshot_json must be an object"
                )
                continue
            snapshots.append((f"{slug}/model_versions[{version_index}]", snapshot))

        for snapshot_label, snapshot in snapshots:
            _validate_snapshot_glossary(
                snapshot, snapshot_label, failures
            )

    if failures:
        details = "\n".join(f"  - {failure}" for failure in failures)
        raise SeedBundleError(
            "seed bundle contains invalid glossary attachment targets:\n" + details
        )


def _validate_snapshot_glossary(
    snapshot: Mapping[str, Any], label: str, failures: list[str]
) -> None:
    """Validate one live or historical model snapshot."""
    target_ids = {
        kind: _ids_for_model(snapshot, plural, label)
        for kind, plural in _ATTACHMENT_TARGETS.items()
    }
    entries = snapshot.get("glossary_entries") or []
    if not isinstance(entries, list):
        failures.append(f"{label}: glossary_entries must be a list")
        return
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            failures.append(
                f"{label}: glossary_entries[{entry_index}] is not an object"
            )
            continue
        attachments = entry.get("attachments") or []
        if not isinstance(attachments, list):
            failures.append(
                f"{label}/{entry.get('term', entry_index)!r}: "
                "attachments must be a list"
            )
            continue
        for attachment_index, attachment in enumerate(attachments):
            if not isinstance(attachment, Mapping):
                failures.append(
                    f"{label}/{entry.get('term', entry_index)!r}: "
                    f"attachments[{attachment_index}] is not an object"
                )
                continue
            target_type = attachment.get("target_type")
            target_id = attachment.get("target_id")
            if target_type == "concept":
                continue
            valid_ids = target_ids.get(str(target_type))
            if valid_ids is None:
                failures.append(
                    f"{label}/{entry.get('term', entry_index)!r}: "
                    f"unsupported glossary target_type {target_type!r}"
                )
            elif target_id is None or str(target_id) not in valid_ids:
                failures.append(
                    f"{label}/{entry.get('term', entry_index)!r}: "
                    f"{target_type!r} target {target_id!r} is not present in "
                    "the model snapshot"
                )


def load_seed_bundle(
    bundle_path: Path,
    *,
    expected_format: str = DEFAULT_EXPORT_FORMAT,
) -> dict[str, Any]:
    """Verify the manifest and schema, then load a trusted seed bundle."""
    bundle_path = Path(bundle_path)
    if not bundle_path.is_file():
        raise SeedBundleError(f"seed bundle missing: {bundle_path}")

    expected_digest = _manifest_digest(bundle_path)
    actual_digest = _sha256(bundle_path)
    if actual_digest != expected_digest:
        raise SeedBundleError(
            f"seed bundle manifest hash mismatch for {bundle_path.name}: "
            f"manifest={expected_digest}, actual={actual_digest}"
        )

    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SeedBundleError(f"seed bundle JSON is unreadable: {bundle_path}") from exc
    if not isinstance(bundle, dict):
        raise SeedBundleError("seed bundle root must be a JSON object")
    if bundle.get("export_format") != expected_format:
        raise SeedBundleError(
            f"bad seed bundle export_format {bundle.get('export_format')!r}; "
            f"expected {expected_format!r}"
        )
    validate_glossary_attachments(bundle)
    return bundle
