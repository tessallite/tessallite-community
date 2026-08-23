from pathlib import Path

from src.dax.tree_sitter_artifacts import resolve_grammar_artifact


def test_windows_uses_cache_dll_even_when_legacy_so_exists(tmp_path: Path):
    grammars = tmp_path / "grammars"
    grammars.mkdir()
    (grammars / "mdx.so").write_bytes(b"linux artifact")

    artifact = resolve_grammar_artifact(
        grammar_name="mdx",
        base_dir=grammars,
        system="Windows",
        machine="AMD64",
    )

    assert artifact.library_path == grammars / ".cache" / "mdx-windows-amd64.dll"
    assert artifact.legacy_library_path == grammars / "mdx.so"
    assert artifact.grammar_dir == grammars / "tree-sitter-mdx"


def test_linux_uses_existing_legacy_so_for_container_path(tmp_path: Path):
    grammars = tmp_path / "grammars"
    grammars.mkdir()
    legacy = grammars / "mdx.so"
    legacy.write_bytes(b"linux artifact")

    artifact = resolve_grammar_artifact(
        grammar_name="mdx",
        base_dir=grammars,
        system="Linux",
        machine="x86_64",
    )

    assert artifact.library_path == legacy


def test_linux_builds_cache_so_when_legacy_so_is_missing(tmp_path: Path):
    grammars = tmp_path / "grammars"
    grammars.mkdir()

    artifact = resolve_grammar_artifact(
        grammar_name="dax",
        base_dir=grammars,
        system="Linux",
        machine="x86_64",
    )

    assert artifact.library_path == grammars / ".cache" / "dax-linux-x86-64.so"
