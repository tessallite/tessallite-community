import json
import sys

import translate_i18n as translate_module

from translate_i18n import (
    diff_locale,
    intentional_identical_ok,
    main,
    merge_all_targets,
    merge_target,
    placeholders_ok,
    protected_tokens_ok,
    translate_file,
)


def snapshot_files(root):
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_diff_locale_does_not_resend_intentional_identical_brand_terms(tmp_path):
    root = tmp_path
    (root / "fr").mkdir()
    (root / "fr" / "common.json").write_text(
        json.dumps(
            {
                "brand.name": "Tessallite",
                "api.label": "API",
                "save.label": "Save",
            }
        ),
        encoding="utf-8",
    )
    source = {
        "common.json": {
            "brand.name": "Tessallite",
            "api.label": "API",
            "save.label": "Save",
        }
    }

    missing = diff_locale(source, root, "fr")

    assert missing == {"common.json": {"save.label": "Save"}}
    assert intentional_identical_ok("Tessallite") is True
    assert intentional_identical_ok("API") is True
    assert intentional_identical_ok("Save") is False


def test_placeholders_ok_rejects_supersets_and_duplicate_count_mismatch():
    assert placeholders_ok("Hello {{name}}", "Bonjour {{name}}") is True
    assert placeholders_ok("Hello {{name}}", "Bonjour {{name}} {{extra}}") is False
    assert placeholders_ok("{{n}} of {{n}}", "{{n}} of") is False
    assert placeholders_ok("{{n}} of", "{{n}} of {{n}}") is False


def test_protected_tokens_ok_preserves_numbers_and_brand_terms():
    assert protected_tokens_ok(
        "Tessallite API limit is 1,000 rows",
        "La limite Tessallite API est de 1,000 lignes",
    ) is True
    assert protected_tokens_ok(
        "Tessallite API limit is 1,000 rows",
        "La limite Tess API est de 1,000 lignes",
    ) is False
    assert protected_tokens_ok(
        "Tessallite API limit is 1,000 rows",
        "La limite Tessallite API est de 999 lignes",
    ) is False


def test_merge_target_prunes_target_only_keys(tmp_path):
    root = tmp_path
    (root / "de").mkdir()
    (root / "de" / "settings.json").write_text(
        json.dumps(
            {
                "settings.current": "Aktuell",
                "settings.removed": "Entfernt",
            }
        ),
        encoding="utf-8",
    )

    added = merge_target(
        root,
        {"settings.current": "Current", "settings.new": "New"},
        "de",
        "settings.json",
        {"settings.new": "Neu"},
    )

    merged = json.loads((root / "de" / "settings.json").read_text(encoding="utf-8"))
    assert added == 1
    assert merged == {
        "settings.current": "Aktuell",
        "settings.new": "Neu",
    }


def test_merge_target_adds_source_only_key_with_english_fallback(tmp_path):
    root = tmp_path
    (root / "fr").mkdir()
    (root / "fr" / "settings.json").write_text(
        json.dumps({"settings.current": "Actuel"}),
        encoding="utf-8",
    )

    added = merge_target(
        root,
        {"settings.current": "Current", "settings.new": "New"},
        "fr",
        "settings.json",
        {},
    )

    merged = json.loads((root / "fr" / "settings.json").read_text(encoding="utf-8"))
    assert added == 0
    assert merged == {
        "settings.current": "Actuel",
        "settings.new": "New",
    }


def test_merge_all_targets_prunes_stale_extra_when_no_keys_are_missing(tmp_path):
    root = tmp_path
    (root / "en").mkdir()
    (root / "de").mkdir()
    source = {
        "settings.json": {
            "settings.current": "Current",
            "settings.ready": "Ready",
        }
    }
    (root / "en" / "settings.json").write_text(
        json.dumps(source["settings.json"]),
        encoding="utf-8",
    )
    (root / "de" / "settings.json").write_text(
        json.dumps(
            {
                "settings.current": "Aktuell",
                "settings.ready": "Bereit",
                "settings.removed": "Entfernt",
            }
        ),
        encoding="utf-8",
    )

    missing = diff_locale(source, root, "de")
    added = merge_all_targets(root, source, ["de"], {})

    merged = json.loads((root / "de" / "settings.json").read_text(encoding="utf-8"))
    assert missing == {}
    assert added == 0
    assert merged == {
        "settings.current": "Aktuell",
        "settings.ready": "Bereit",
    }


def test_translate_file_reports_unresolved_keys_after_exception_exhaustion(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fail_transport(*_args, **_kwargs):
        calls.append("attempt")
        raise RuntimeError("transport down")

    monkeypatch.setattr(translate_module, "call_llm", fail_transport)
    todo = {
        "settings.one": "One",
        "settings.two": "Two {{count}}",
    }

    translations, failed = translate_file(
        "openai",
        "test-model",
        "test-key",
        "https://example.test",
        "fr",
        "settings.json",
        todo,
        20,
        1000,
        1,
        tmp_path,
    )

    (tmp_path / "fr").mkdir()
    added = merge_target(tmp_path, todo, "fr", "settings.json", translations)
    merged = json.loads((tmp_path / "fr" / "settings.json").read_text(encoding="utf-8"))

    assert len(calls) == 3
    assert translations == {}
    assert failed == ["settings.one", "settings.two"]
    assert added == 0
    assert merged == todo


def test_translate_file_retries_unchanged_non_protected_output_but_accepts_protected(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_call_llm(*_args, **_kwargs):
        calls.append("attempt")
        return {}

    def fake_extract_text(*_args, **_kwargs):
        return json.dumps(
            {
                "brand.name": "Tessallite",
                "action.save": "Save",
            }
        )

    monkeypatch.setattr(translate_module, "call_llm", fake_call_llm)
    monkeypatch.setattr(translate_module, "extract_text", fake_extract_text)
    todo = {
        "brand.name": "Tessallite",
        "action.save": "Save",
    }

    translations, failed = translate_file(
        "openai",
        "test-model",
        "test-key",
        "https://example.test",
        "fr",
        "settings.json",
        todo,
        20,
        1000,
        1,
        tmp_path,
    )

    (tmp_path / "fr").mkdir()
    added = merge_target(tmp_path, todo, "fr", "settings.json", translations)
    merged = json.loads((tmp_path / "fr" / "settings.json").read_text(encoding="utf-8"))

    assert len(calls) == 3
    assert translations == {"brand.name": "Tessallite"}
    assert failed == ["action.save"]
    assert added == 1
    assert merged == {
        "brand.name": "Tessallite",
        "action.save": "Save",
    }


def test_dry_run_writes_no_reports_catalogs_temp_or_fallback_files(
    monkeypatch,
    tmp_path,
    capsys,
):
    (tmp_path / "en").mkdir()
    (tmp_path / "de").mkdir()
    (tmp_path / "en" / "settings.json").write_text(
        json.dumps(
            {
                "settings.current": "Current",
                "settings.new": "New",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "de" / "settings.json").write_text(
        json.dumps({"settings.current": "Aktuell"}),
        encoding="utf-8",
    )
    before = snapshot_files(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "translate_i18n.py",
            "--root",
            str(tmp_path),
            "--targets",
            "de",
            "--dry-run",
        ],
    )

    result = main()
    after = snapshot_files(tmp_path)
    output = capsys.readouterr().out

    assert result == 0
    assert after == before
    assert not (tmp_path / "de" / "missing-keys.md").exists()
    assert not (tmp_path / ".i18n-translate-tmp").exists()
    assert "settings.new" in output
    assert "Dry run: no files written" in output
