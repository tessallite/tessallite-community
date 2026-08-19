"""Unit tests for shared.git.model_repo.

Tests run real git commands against temporary directories (``tmp_path``).
They require ``git`` on PATH; if missing the entire module is skipped.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

# Skip the entire module if git is not available
try:
    subprocess.run(
        ["git", "--version"],
        capture_output=True, timeout=10, check=True,
    )
except (FileNotFoundError, subprocess.SubprocessError):
    pytest.skip("git CLI not available", allow_module_level=True)

from tessallite.shared.git.model_repo import (
    GitError,
    commit_layout,
    commit_model,
    commit_restore,
    get_diff,
    get_file_at,
    get_log,
    init_tenant_repo,
    tag_deploy,
)


@pytest.fixture(autouse=True)
def _git_data_dir(tmp_path, monkeypatch):
    """Point every test at a private temp directory."""
    monkeypatch.setenv("TESSALLITE_GIT_DATA_DIR", str(tmp_path))


# ---- Slug / SHA / filename validation ----

TENANT = "acme_demo"
MODEL = "sales_model"


class TestSlugValidation:
    """Slug validation must mirror the product's own slug contracts.

    Tenant slugs are ``^[a-z0-9_-]+$`` (TenantCreate); model slugs are
    ``^[A-Za-z_][A-Za-z0-9_]*$`` (the BI-safe contract).  Anything outside
    those is rejected; anything inside them must be accepted (Bug-8687).
    """

    @pytest.mark.parametrize("bad_slug", [
        "UPPER", "has space", "../traversal", "", "a/b", "a.b",
        "acme_demo\n", "x" * 65,
    ])
    def test_invalid_tenant_slug(self, bad_slug):
        with pytest.raises(ValueError, match="Invalid tenant_slug"):
            init_tenant_repo(bad_slug)

    @pytest.mark.parametrize("bad_slug", [
        "has-dash", "has space", "../up", "", "a/b", "9leading_digit",
        "sales_model\n", "x" * 65,
    ])
    def test_invalid_model_slug(self, bad_slug):
        init_tenant_repo(TENANT)
        with pytest.raises(ValueError, match="Invalid model_slug"):
            get_log(TENANT, bad_slug)


class TestSlugContractParity:
    """Bug-8687: a hyphenated tenant slug must not disable git tracking.

    The demo tenant's slug is literally ``acme-demo``.  ``_SLUG_RE`` was
    ``^[a-z0-9_]+$``, so every hyphenated tenant and every capitalised model
    slug raised ``ValueError`` inside the git helpers -- and because all six
    model-service call sites swallow that as "non-fatal", the user simply saw
    an empty Version History panel, an empty diff and no deploy tag, forever.

    These tests pin this module against the CANONICAL contracts rather than
    restating a charset, so a future narrowing on either side fails here.
    """

    def test_accepts_the_hyphenated_demo_tenant_slug(self, tmp_path):
        """The exact slug that was broken in production."""
        repo = init_tenant_repo("acme-demo")
        assert (repo / ".git").is_dir()
        assert repo == tmp_path / "acme-demo"

    def test_full_round_trip_for_a_hyphenated_tenant(self):
        """Commit, tag a deploy, and read the log back for ``acme-demo``."""
        init_tenant_repo("acme-demo")
        sha = commit_model(
            tenant_slug="acme-demo",
            model_slug="sales_model",
            model_yaml="model: {}\n",
            layout_json={"nodes": []},
            summary="first",
            author_email="admin@acme-demo.com",
            version_number=1,
        )
        assert sha
        tag_deploy("acme-demo", "sales_model", 1)
        entries = get_log("acme-demo", "sales_model")
        assert len(entries) == 1
        assert "deploy/v1" in entries[0]["tags"]

    def test_accepts_a_capitalised_model_slug(self):
        """``SalesModel`` is a legal BI-safe model slug."""
        init_tenant_repo(TENANT)
        sha = commit_model(
            tenant_slug=TENANT,
            model_slug="SalesModel",
            model_yaml="model: {}\n",
            layout_json={},
            summary=None,
            author_email="a@b.c",
            version_number=1,
        )
        assert sha

    def test_tenant_charset_is_not_narrower_than_tenant_create(self):
        """Every slug ``TenantCreate`` would accept must reach the repo."""
        from tessallite.shared.git.model_repo import _TENANT_SLUG_RE
        from tessallite.shared.schemas.domains.tenants_projects import TenantCreate

        contract = TenantCreate.model_fields["slug"].metadata
        pattern = next(
            m.pattern for m in contract if hasattr(m, "pattern")
        )
        assert pattern == r"^[a-z0-9_-]+$", (
            "TenantCreate.slug pattern moved; update _TENANT_SLUG_RE to match"
        )
        for slug in ("acme-demo", "a-b-c", "tenant_1", "x", "0"):
            assert re.fullmatch(pattern, slug), f"fixture {slug!r} is off-contract"
            assert _TENANT_SLUG_RE.fullmatch(slug), (
                f"model_repo rejects {slug!r}, which TenantCreate accepts"
            )

    def test_model_charset_is_not_narrower_than_the_bi_safe_contract(self):
        """Every slug the BI-safe validator accepts must reach the repo."""
        from tessallite.shared.git.model_repo import _MODEL_SLUG_RE
        from tessallite.shared.model_snapshot.slug_utils import (
            validate_bi_safe_slug,
        )

        for slug in ("modelx", "SalesModel", "Sales_2024", "_private", "M"):
            validate_bi_safe_slug(slug)  # raises if the fixture is off-contract
            assert _MODEL_SLUG_RE.fullmatch(slug), (
                f"model_repo rejects {slug!r}, which the BI-safe contract accepts"
            )


class TestShaValidation:
    def test_rejects_short(self):
        with pytest.raises(ValueError, match="Invalid commit SHA"):
            get_diff(TENANT, MODEL, "abc", "1234abcd")

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError, match="Invalid commit SHA"):
            get_diff(TENANT, MODEL, "ABCD1234", "1234abcd")

    def test_rejects_non_hex(self):
        with pytest.raises(ValueError, match="Invalid commit SHA"):
            get_diff(TENANT, MODEL, "zzzz", "1234abcd")


class TestFilenameWhitelist:
    def test_rejects_arbitrary_filename(self):
        with pytest.raises(ValueError, match="not allowed"):
            get_file_at(TENANT, MODEL, "abcd1234", "secrets.env")

    def test_allows_model_yaml(self):
        # Will fail at the git level (no repo), but passes validation
        init_tenant_repo(TENANT)
        with pytest.raises(GitError):
            get_file_at(TENANT, MODEL, "abcd1234", "model.yaml")

    def test_allows_canvas_layout(self):
        init_tenant_repo(TENANT)
        with pytest.raises(GitError):
            get_file_at(TENANT, MODEL, "abcd1234", "canvas_layout.json")


# ---- Init ----

class TestInitTenantRepo:
    def test_creates_repo(self, tmp_path):
        path = init_tenant_repo(TENANT)
        assert (path / ".git").is_dir()
        assert path == tmp_path / TENANT

    def test_idempotent(self, tmp_path):
        p1 = init_tenant_repo(TENANT)
        p2 = init_tenant_repo(TENANT)
        assert p1 == p2
        assert (p2 / ".git").is_dir()

    def test_sets_git_config(self):
        repo = init_tenant_repo(TENANT)
        name = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, cwd=str(repo),
        )
        email = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert name.stdout.strip() == "Tessallite"
        assert email.stdout.strip() == "system@tessallite.local"


# ---- Commit layout ----

class TestCommitLayout:
    def test_creates_commit(self):
        layout = {"nodes": [{"id": "1"}], "edges": []}
        sha = commit_layout(TENANT, MODEL, layout, "initial layout", "dev@co.com")

        assert len(sha) >= 4
        repo = init_tenant_repo(TENANT)

        # File exists with correct content
        f = repo / "models" / MODEL / "canvas_layout.json"
        assert f.exists()
        assert json.loads(f.read_text()) == layout

        # Commit message matches
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "[layout] initial layout" in log.stdout

    def test_default_summary(self):
        sha = commit_layout(TENANT, MODEL, {}, None, "dev@co.com")
        repo = init_tenant_repo(TENANT)
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "[layout] layout update" in log.stdout


# ---- Commit model ----

class TestCommitModel:
    def test_creates_commit_with_tag(self):
        yaml_content = "name: sales\nmeasures:\n  - revenue\n"
        layout = {"nodes": [], "edges": []}
        sha = commit_model(
            TENANT, MODEL, yaml_content, layout,
            "first version", "dev@co.com", version_number=1,
        )

        assert len(sha) >= 4
        repo = init_tenant_repo(TENANT)

        # Both files exist
        assert (repo / "models" / MODEL / "model.yaml").exists()
        assert (repo / "models" / MODEL / "canvas_layout.json").exists()

        # Tag exists
        tags = subprocess.run(
            ["git", "tag", "-l", "v1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "v1" in tags.stdout

        # Commit message
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "[model] v1: first version" in log.stdout

    def test_model_yaml_content_preserved(self):
        yaml_content = "dimensions:\n  - date\n  - region\n"
        commit_model(
            TENANT, MODEL, yaml_content, {},
            None, "dev@co.com", version_number=1,
        )
        repo = init_tenant_repo(TENANT)
        stored = (repo / "models" / MODEL / "model.yaml").read_text()
        assert stored == yaml_content


# ---- Commit restore ----

class TestCommitRestore:
    def test_creates_restore_commit(self):
        # First commit a model to have something to restore from
        commit_model(
            TENANT, MODEL, "v1: original", {},
            "original", "dev@co.com", version_number=1,
        )
        sha = commit_restore(
            TENANT, MODEL, "v1: original", {},
            "dev@co.com", restored_from=1, new_version=2,
        )
        assert len(sha) >= 4

        repo = init_tenant_repo(TENANT)
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "[restore] from v1" in log.stdout

        tags = subprocess.run(
            ["git", "tag", "-l", "v2"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "v2" in tags.stdout


# ---- Tag deploy ----

class TestTagDeploy:
    def test_creates_deploy_tag(self):
        commit_model(
            TENANT, MODEL, "v1", {}, "v1", "dev@co.com", version_number=1,
        )
        tag_deploy(TENANT, MODEL, version_number=1)

        repo = init_tenant_repo(TENANT)
        tags = subprocess.run(
            ["git", "tag", "-l", "deploy/v1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "deploy/v1" in tags.stdout

    def test_force_replaces_existing(self):
        commit_model(
            TENANT, MODEL, "v1", {}, "v1", "dev@co.com", version_number=1,
        )
        tag_deploy(TENANT, MODEL, version_number=1)
        # Should not raise when called again
        tag_deploy(TENANT, MODEL, version_number=1)

        repo = init_tenant_repo(TENANT)
        tags = subprocess.run(
            ["git", "tag", "-l", "deploy/v1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert tags.stdout.strip().count("deploy/v1") == 1


# ---- Get log ----

def _seed_multiple_commits() -> None:
    """Create several commits for log/pagination tests."""
    commit_model(TENANT, MODEL, "v1", {}, "first", "a@co.com", 1)
    commit_layout(TENANT, MODEL, {"v": 2}, "tweak layout", "b@co.com")
    commit_model(TENANT, MODEL, "v2", {"v": 2}, "second", "a@co.com", 2)
    commit_restore(TENANT, MODEL, "v1", {}, "a@co.com", 1, 3)


class TestGetLog:
    def test_returns_correct_structure(self):
        _seed_multiple_commits()
        entries = get_log(TENANT, MODEL)

        assert len(entries) == 4

        # Most recent first (restore)
        assert entries[0]["type"] == "restore"
        assert entries[0]["version"] == 1  # "from v1"
        assert entries[0]["author"] == "a@co.com"
        assert "timestamp" in entries[0]
        assert len(entries[0]["sha"]) == 40  # full SHA from %H

        # Second is model v2
        assert entries[1]["type"] == "model"
        assert entries[1]["version"] == 2

        # Third is layout
        assert entries[2]["type"] == "layout"
        assert entries[2]["version"] is None

        # Fourth is model v1
        assert entries[3]["type"] == "model"
        assert entries[3]["version"] == 1

    def test_empty_repo_returns_empty(self):
        init_tenant_repo(TENANT)
        entries = get_log(TENANT, MODEL)
        assert entries == []

    def test_nonexistent_repo_returns_empty(self):
        entries = get_log(TENANT, MODEL)
        assert entries == []

    def test_tags_in_log(self):
        commit_model(TENANT, MODEL, "v1", {}, "first", "a@co.com", 1)
        entries = get_log(TENANT, MODEL)
        assert len(entries) == 1
        assert "v1" in entries[0]["tags"]


class TestGetLogPagination:
    def test_limit(self):
        _seed_multiple_commits()
        entries = get_log(TENANT, MODEL, limit=2)
        assert len(entries) == 2

    def test_offset(self):
        _seed_multiple_commits()
        all_entries = get_log(TENANT, MODEL)
        offset_entries = get_log(TENANT, MODEL, limit=2, offset=2)
        assert len(offset_entries) == 2
        assert offset_entries[0]["sha"] == all_entries[2]["sha"]
        assert offset_entries[1]["sha"] == all_entries[3]["sha"]

    def test_offset_beyond_end(self):
        _seed_multiple_commits()
        entries = get_log(TENANT, MODEL, offset=100)
        assert entries == []


# ---- Get diff ----

class TestGetDiff:
    def test_returns_diff(self):
        commit_model(TENANT, MODEL, "v1 content\n", {}, "v1", "a@co.com", 1)
        commit_model(TENANT, MODEL, "v2 content\n", {}, "v2", "a@co.com", 2)

        log = get_log(TENANT, MODEL)
        sha_v2 = log[0]["sha"]
        sha_v1 = log[1]["sha"]

        diff = get_diff(TENANT, MODEL, sha_v1, sha_v2)
        assert "v1 content" in diff
        assert "v2 content" in diff
        assert "diff --git" in diff


# ---- Get file at ----

class TestGetFileAt:
    def test_reads_correct_content(self):
        yaml_v1 = "name: sales_v1\n"
        commit_model(TENANT, MODEL, yaml_v1, {"v": 1}, "v1", "a@co.com", 1)

        yaml_v2 = "name: sales_v2\n"
        commit_model(TENANT, MODEL, yaml_v2, {"v": 2}, "v2", "a@co.com", 2)

        log = get_log(TENANT, MODEL)
        sha_v1 = log[1]["sha"]

        content = get_file_at(TENANT, MODEL, sha_v1, "model.yaml")
        assert content.strip() == "name: sales_v1"

    def test_reads_layout_json(self):
        layout = {"nodes": [{"id": "n1"}]}
        commit_model(TENANT, MODEL, "v1\n", layout, "v1", "a@co.com", 1)

        log = get_log(TENANT, MODEL)
        sha = log[0]["sha"]
        content = get_file_at(TENANT, MODEL, sha, "canvas_layout.json")
        assert json.loads(content) == layout


# ---- Sanitise commit message ----

class TestSanitiseCommitMessage:
    def test_strips_control_chars(self):
        layout = {"a": 1}
        sha = commit_layout(
            TENANT, MODEL, layout,
            "hello\x00world\x07test",
            "dev@co.com",
        )
        repo = init_tenant_repo(TENANT)
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert "\x00" not in log.stdout
        assert "\x07" not in log.stdout
        assert "helloworldtest" in log.stdout

    def test_truncates_long_message(self):
        long_msg = "x" * 600
        # Commit won't fail; the message is just truncated
        commit_layout(TENANT, MODEL, {"a": 1}, long_msg, "dev@co.com")
        repo = init_tenant_repo(TENANT)
        log = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            capture_output=True, text=True, cwd=str(repo),
        )
        # [layout] prefix + space + 500 chars total max
        assert len(log.stdout.strip()) <= 500


# ---- Push remote ----
# Skipped: requires a real remote. Needs integration testing.

@pytest.mark.skip(reason="Requires a real git remote -- integration test only")
class TestPushRemote:
    def test_push_remote(self):
        pass
