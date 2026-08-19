"""Git-based model version control for Tessallite tenants.

Each tenant gets its own git repository under DATA_DIR/<tenant-slug>/.
Model files live at models/<model-slug>/{model.yaml, canvas_layout.json}.
All git operations use subprocess with list-form args (never shell=True).

Internal module -- never exposed directly via REST.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bug-8687: these charsets must mirror the product's own slug contracts, not a
# narrower guess.  The previous single `^[a-z0-9_]+$` rejected every hyphenated
# tenant slug -- the demo tenant is literally ``acme-demo`` -- and every model
# slug carrying a capital.  Because all six call sites in model-service treat a
# git failure as non-fatal (`except Exception: logger.warning(...)`), the effect
# was that git version tracking was silently OFF for those tenants and models:
# an empty Version History panel, an empty diff, and no deploy tag, with only a
# warning in the log.  Sources of truth:
#   tenant slug -> shared/schemas/domains/tenants_projects.py :: TenantCreate.slug
#   model slug  -> shared/model_snapshot/slug_utils.py        :: _BI_SAFE_SLUG_RE
# ``tests/test_model_repo.py::TestSlugContractParity`` imports both of those and
# fails if this module ever diverges from them again.
_SLUG_MAX_LEN = 64
_TENANT_SLUG_RE = re.compile(r"[a-z0-9_-]+")
_MODEL_SLUG_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
_ALLOWED_FILENAMES = frozenset({"model.yaml", "canvas_layout.json"})
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_MESSAGE_LEN = 500

# Token patterns to strip from error output before logging
_TOKEN_IN_URL_RE = re.compile(r"(https?://)([^@]+)@")


def _data_dir() -> Path:
    """Resolve the root data directory for tenant git repos."""
    env = os.environ.get("TESSALLITE_GIT_DATA_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[3] / "data" / "git"


def _validate_slug(
    value: str, label: str, pattern: re.Pattern[str], charset: str,
) -> None:
    """Reject a slug that is not a single safe path component.

    ``fullmatch`` rather than ``match``: ``$`` also matches immediately before
    a trailing newline, so ``re.match`` accepted ``"acme_demo\\n"`` as a slug
    and carried it into a filesystem path.
    """
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(
            f"Invalid {label}: {value!r}. Must match {charset}."
        )
    if len(value) > _SLUG_MAX_LEN:
        raise ValueError(
            f"Invalid {label}: {value!r}. "
            f"Must be at most {_SLUG_MAX_LEN} characters."
        )


def _validate_tenant_slug(value: str) -> None:
    _validate_slug(value, "tenant_slug", _TENANT_SLUG_RE, "[a-z0-9_-]+")


def _validate_model_slug(value: str) -> None:
    _validate_slug(value, "model_slug", _MODEL_SLUG_RE, "[A-Za-z_][A-Za-z0-9_]*")


def _validate_sha(sha: str) -> None:
    if not _SHA_RE.match(sha):
        raise ValueError(
            f"Invalid commit SHA: {sha!r}. Must be 4-40 lowercase hex characters."
        )


def _validate_filename(filename: str) -> None:
    if filename not in _ALLOWED_FILENAMES:
        raise ValueError(
            f"Filename {filename!r} not allowed. "
            f"Must be one of: {', '.join(sorted(_ALLOWED_FILENAMES))}"
        )


def _sanitise_message(msg: str) -> str:
    """Strip control characters and truncate commit messages."""
    cleaned = _CONTROL_CHAR_RE.sub("", msg)
    if len(cleaned) > _MAX_MESSAGE_LEN:
        cleaned = cleaned[:_MAX_MESSAGE_LEN]
    return cleaned


def _strip_token(text: str) -> str:
    """Remove auth tokens from URLs in error output."""
    return _TOKEN_IN_URL_RE.sub(r"\1***@", text)


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment for git subprocesses.

    Only PATH and HOME propagate from the host.  Git identity is set
    per-repo via config, so GIT_AUTHOR/COMMITTER vars are only added
    when callers supply them through ``extra``.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class GitError(Exception):
    """Raised when a git subprocess exits with a non-zero return code."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        # Show only the git subcommand for readability
        subcmd = cmd[1] if len(cmd) > 1 else "?"
        super().__init__(
            f"git {subcmd} failed (rc={returncode}): {stderr[:200]}"
        )


# ---------------------------------------------------------------------------
# Low-level git helper
# ---------------------------------------------------------------------------

def _git(
    repo_path: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in *repo_path* with a locked-down environment.

    Raises GitError on non-zero exit.
    """
    cmd = ["git"] + list(args)
    # Log without tokens
    safe_cmd = [_strip_token(c) for c in cmd]
    logger.debug("git: %s (cwd=%s)", safe_cmd, repo_path)

    env = _clean_env(env_extra)
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=30,
        cwd=str(repo_path),
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise GitError(safe_cmd, result.returncode, result.stderr.strip())
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_tenant_repo(tenant_slug: str) -> Path:
    """Initialise a git repo for a tenant.  Idempotent."""
    _validate_tenant_slug(tenant_slug)

    base = _data_dir()
    repo = (base / tenant_slug).resolve()
    if not str(repo).startswith(str(base)):
        raise ValueError("Path traversal detected")

    repo.mkdir(parents=True, exist_ok=True)

    git_dir = repo / ".git"
    if git_dir.is_dir():
        logger.debug("Repo already exists: %s", repo)
        return repo

    _git(repo, "init")
    _git(repo, "config", "user.name", "Tessallite")
    _git(repo, "config", "user.email", "system@tessallite.local")
    logger.info("Initialised tenant repo: %s", repo)
    return repo


def _ensure_repo(tenant_slug: str) -> Path:
    """Lazy-init wrapper used by commit helpers."""
    return init_tenant_repo(tenant_slug)


def _model_dir(repo: Path, model_slug: str) -> Path:
    """Return the models/<model-slug>/ directory, creating it if needed."""
    _validate_model_slug(model_slug)
    model_path = (repo / "models" / model_slug).resolve()
    if not str(model_path).startswith(str(repo.resolve())):
        raise ValueError("Path traversal detected")
    model_path.mkdir(parents=True, exist_ok=True)
    return model_path


def _author_env(author_email: str) -> dict[str, str]:
    """Build env vars to set the commit author."""
    return {
        "GIT_AUTHOR_NAME": author_email,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": "Tessallite",
        "GIT_COMMITTER_EMAIL": "system@tessallite.local",
    }


def commit_layout(
    tenant_slug: str,
    model_slug: str,
    layout_json: dict,
    summary: str | None,
    author_email: str,
) -> str:
    """Commit a canvas layout update.  Returns the short commit SHA."""
    repo = _ensure_repo(tenant_slug)
    model_path = _model_dir(repo, model_slug)

    layout_file = model_path / "canvas_layout.json"
    layout_file.write_text(
        json.dumps(layout_json, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    rel_path = f"models/{model_slug}/canvas_layout.json"
    _git(repo, "add", rel_path)

    raw_summary = summary or "layout update"
    message = _sanitise_message(f"[layout] {raw_summary}")
    env = _author_env(author_email)

    _git(
        repo, "commit", "-m", message,
        f"--author={author_email} <{author_email}>",
        env_extra=env,
    )

    result = _git(repo, "rev-parse", "--short", "HEAD")
    return result.stdout.strip()


def commit_model(
    tenant_slug: str,
    model_slug: str,
    model_yaml: str,
    layout_json: dict,
    summary: str | None,
    author_email: str,
    version_number: int,
) -> str:
    """Commit a model version (YAML + layout).  Tags as v{version_number}.

    Returns the short commit SHA.
    """
    repo = _ensure_repo(tenant_slug)
    model_path = _model_dir(repo, model_slug)

    # Write files
    (model_path / "model.yaml").write_text(model_yaml, encoding="utf-8")
    (model_path / "canvas_layout.json").write_text(
        json.dumps(layout_json, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (model_path / ".version").write_text(
        json.dumps({"version": version_number}) + "\n", encoding="utf-8",
    )

    rel_yaml = f"models/{model_slug}/model.yaml"
    rel_layout = f"models/{model_slug}/canvas_layout.json"
    rel_version = f"models/{model_slug}/.version"
    _git(repo, "add", rel_yaml, rel_layout, rel_version)

    raw_summary = summary or "model update"
    message = _sanitise_message(f"[model] v{version_number}: {raw_summary}")
    env = _author_env(author_email)

    _git(
        repo, "commit", "-m", message,
        f"--author={author_email} <{author_email}>",
        env_extra=env,
    )

    tag_name = f"v{version_number}"
    _git(repo, "tag", tag_name)

    result = _git(repo, "rev-parse", "--short", "HEAD")
    return result.stdout.strip()


def commit_restore(
    tenant_slug: str,
    model_slug: str,
    model_yaml: str,
    layout_json: dict,
    author_email: str,
    restored_from: int,
    new_version: int,
) -> str:
    """Commit a model restore from a prior version.  Returns short SHA."""
    repo = _ensure_repo(tenant_slug)
    model_path = _model_dir(repo, model_slug)

    (model_path / "model.yaml").write_text(model_yaml, encoding="utf-8")
    (model_path / "canvas_layout.json").write_text(
        json.dumps(layout_json, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Write a version marker so git always sees a diff even when
    # restoring to identical content (ensures git log --path picks it up)
    version_meta = {"version": new_version, "restored_from": restored_from}
    (model_path / ".version").write_text(
        json.dumps(version_meta) + "\n", encoding="utf-8",
    )

    rel_yaml = f"models/{model_slug}/model.yaml"
    rel_layout = f"models/{model_slug}/canvas_layout.json"
    rel_version = f"models/{model_slug}/.version"
    _git(repo, "add", rel_yaml, rel_layout, rel_version)

    message = _sanitise_message(f"[restore] from v{restored_from}")
    env = _author_env(author_email)

    _git(
        repo, "commit", "-m", message,
        f"--author={author_email} <{author_email}>",
        env_extra=env,
    )

    tag_name = f"v{new_version}"
    _git(repo, "tag", tag_name)

    result = _git(repo, "rev-parse", "--short", "HEAD")
    return result.stdout.strip()


def tag_deploy(
    tenant_slug: str,
    model_slug: str,
    version_number: int,
) -> None:
    """Create a deploy tag pointing at the version commit.

    Overwrites an existing deploy tag for the same version.
    """
    _validate_tenant_slug(tenant_slug)
    _validate_model_slug(model_slug)

    repo = _data_dir() / tenant_slug
    if not (repo / ".git").is_dir():
        raise GitError(
            ["git", "tag"], 1,
            f"No repository for tenant {tenant_slug!r}",
        )

    version_tag = f"v{version_number}"
    deploy_tag = f"deploy/v{version_number}"

    # Resolve the commit the version tag points at
    result = _git(repo, "rev-parse", version_tag)
    target_sha = result.stdout.strip()

    # Force-create the deploy tag (delete first if it exists)
    try:
        _git(repo, "tag", "-d", deploy_tag)
    except GitError:
        pass  # tag did not exist yet
    _git(repo, "tag", deploy_tag, target_sha)


def get_log(
    tenant_slug: str,
    model_slug: str,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return structured commit log entries for a model.

    Each entry: {sha, type, message, version, tags, author, timestamp}.
    """
    _validate_tenant_slug(tenant_slug)
    _validate_model_slug(model_slug)

    repo = _data_dir() / tenant_slug
    if not (repo / ".git").is_dir():
        return []

    # Field separator unlikely to appear in messages
    sep = "\x1f"
    fmt = sep.join(["%H", "%s", "%an", "%aI", "%D"])

    try:
        result = _git(
            repo,
            "log",
            f"--format={fmt}",
            f"--skip={offset}",
            f"-n{limit}",
            "--",
            f"models/{model_slug}/",
        )
    except GitError:
        # No commits yet, or path never committed
        return []

    entries: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(sep, 4)
        if len(parts) < 5:
            continue

        sha, subject, author, timestamp, refs = parts

        # Determine type from commit message prefix
        commit_type = "model"
        if subject.startswith("[layout]"):
            commit_type = "layout"
        elif subject.startswith("[restore]"):
            commit_type = "restore"

        # Extract version number from the message
        version: int | None = None
        ver_match = re.search(r"\bv(\d+)", subject)
        if ver_match and commit_type != "layout":
            version = int(ver_match.group(1))

        # Parse tags from the refs decoration
        tags: list[str] = []
        if refs.strip():
            for ref_part in refs.split(","):
                ref_part = ref_part.strip()
                if ref_part.startswith("tag: "):
                    tags.append(ref_part[5:].strip())

        entries.append({
            "sha": sha,
            "type": commit_type,
            "message": subject,
            "version": version,
            "tags": tags,
            "author": author,
            "timestamp": timestamp,
        })

    return entries


def get_diff(
    tenant_slug: str,
    model_slug: str,
    sha1: str,
    sha2: str,
) -> str:
    """Return the unified diff between two commits for a model's files."""
    _validate_tenant_slug(tenant_slug)
    _validate_model_slug(model_slug)
    _validate_sha(sha1)
    _validate_sha(sha2)

    repo = _data_dir() / tenant_slug
    result = _git(
        repo, "diff", sha1, sha2, "--", f"models/{model_slug}/",
    )
    return result.stdout


def get_file_at(
    tenant_slug: str,
    model_slug: str,
    sha: str,
    filename: str,
) -> str:
    """Return the content of a model file at a specific commit."""
    _validate_tenant_slug(tenant_slug)
    _validate_model_slug(model_slug)
    _validate_sha(sha)
    _validate_filename(filename)

    repo = _data_dir() / tenant_slug
    object_path = f"{sha}:models/{model_slug}/{filename}"
    result = _git(repo, "show", object_path)
    return result.stdout


def push_remote(
    tenant_slug: str,
    remote_url: str,
    token: str,
) -> bool:
    """Push the tenant repo to a remote.  Returns True on success.

    The token is injected into the HTTPS URL for authentication and is
    never logged, stored in config, or included in error messages.
    """
    _validate_tenant_slug(tenant_slug)

    repo = _data_dir() / tenant_slug
    if not (repo / ".git").is_dir():
        logger.error("push_remote: no repo for tenant %r", tenant_slug)
        return False

    # Build authenticated URL
    if "://" in remote_url:
        scheme, rest = remote_url.split("://", 1)
        auth_url = f"{scheme}://{token}@{rest}"
    else:
        logger.error("push_remote: remote_url must be HTTPS")
        return False

    try:
        # Set/update the origin remote
        try:
            _git(repo, "remote", "set-url", "origin", auth_url)
        except GitError:
            _git(repo, "remote", "add", "origin", auth_url)

        _git(repo, "push", "origin", "main", "--tags")
        logger.info("push_remote: pushed tenant %r successfully", tenant_slug)
        return True
    except GitError as exc:
        # Strip any token from the error before logging
        safe_stderr = _strip_token(exc.stderr)
        logger.error(
            "push_remote failed for tenant %r: %s",
            tenant_slug, safe_stderr,
        )
        return False
    finally:
        # Remove the token from the stored remote URL
        try:
            _git(repo, "remote", "set-url", "origin", remote_url)
        except GitError:
            pass
