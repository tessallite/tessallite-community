"""Named Query resolution for the SQL query path.

A Named Query is referenced as ``SELECT * FROM @Name`` (the whole statement is
the reference — invariant 5). This module:

1. loads Named Query DEFINITIONS from the deployed snapshot (invariant 7:
   governed model content; an edit needs a deploy), cached per
   ``(model_id, deployed_version_id)``;
2. recognises the exact reference shape on the raw SQL (token-level, never
   text regex — an ``@name`` inside a string literal is never touched, and the
   shape check does NOT rely on how sqlglot parses ``@x`` in FROM position);
3. decides materialised-first vs live (source-fallback) serving.

Security is CONSUMED, never authored (invariant 4): the projection-shape
materialised proof reuses the pocket RLS rules verbatim (row-preserving
definition, every security column materialised in the row manifest,
case-sensitive match, no user_mapping rule); an aggregated-shape Named Query
under active row security always falls back to LIVE execution under the
consumer's security context. CLS-active principals fall back to live for the
materialised fast path in v1 (no column-projection of a shared cache).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import sqlglot
from sqlglot.tokens import TokenType

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300
_MAX_CACHE_ENTRIES = 256


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NamedQueryError(ValueError):
    """Base for named-query resolution failures, surfaced as a 400."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class NamedQueryUnknownReference(NamedQueryError):
    def __init__(self, name: str):
        super().__init__(
            "NQ_UNKNOWN_REFERENCE",
            f"Unknown Named Query reference: @{name}. No Named Query with "
            f"this name is deployed on the model.",
        )


class NamedQueryWrongType(NamedQueryError):
    def __init__(self, name: str, object_kind: str):
        super().__init__(
            "NQ_WRONG_TYPE",
            f"'@{name}' is a {object_kind}, not a Named Query. Named "
            f"Queries are referenced as SELECT * FROM @Name; named lists "
            f"resolve inside IN (...) predicates.",
        )


class NamedQueryUnsupportedShape(NamedQueryError):
    def __init__(self, name: str):
        super().__init__(
            "NQ_UNSUPPORTED_SHAPE",
            f"Unsupported Named Query reference shape for @{name}: v1 "
            f"accepts only 'SELECT * FROM @{name}' as the whole statement. "
            f"Projection subsets, joins, WHERE against the reference and "
            f"nested references are not supported.",
        )


# ---------------------------------------------------------------------------
# Snapshot-backed definition cache
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NamedQueryDefinition:
    """A Named Query definition read from the deployed snapshot."""

    id: str
    name: str
    definition_sql: str
    output_columns: tuple = ()
    shape: str = "projection"
    row_cap: Optional[int] = None
    column_cap: Optional[int] = None
    artifact: Optional[dict] = None  # identity pointer, None = never refreshed

    def __post_init__(self):
        if isinstance(self.output_columns, list):
            object.__setattr__(self, "output_columns", tuple(self.output_columns))


# key -> (expires_at, {lower_@name: NamedQueryDefinition})
_NAMED_QUERY_CACHE: dict[
    tuple[str, str, int], tuple[float, dict[str, NamedQueryDefinition]]
] = {}


def invalidate_named_query_cache(model_id: Optional[str] = None) -> None:
    """Drop cached named queries. No arg clears everything (test hook)."""
    if model_id is None:
        _NAMED_QUERY_CACHE.clear()
        return
    mid = str(model_id)
    for key in [k for k in _NAMED_QUERY_CACHE if k[0] == mid]:
        _NAMED_QUERY_CACHE.pop(key, None)


def _extract_named_queries_from_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, NamedQueryDefinition]:
    """Build a case-insensitive ``{lower_@name: definition}`` lookup."""
    result: dict[str, NamedQueryDefinition] = {}
    for nq in snapshot.get("named_queries", []) or []:
        if not isinstance(nq, dict):
            continue
        name = nq.get("name")
        if not name:
            continue
        key = f"@{name}".lower() if not name.startswith("@") else name.lower()
        if key in result:
            # Fail closed on duplicate lowercase keys (legacy data).
            raise NamedQueryError(
                "NQ_DUPLICATE_NAME",
                f"Deployed snapshot contains duplicate lowercase Named Query "
                f"key '{key}'. Remove or rename the duplicate before deploying.",
            )
        result[key] = NamedQueryDefinition(
            id=str(nq.get("id") or ""),
            name=name,
            definition_sql=str(nq.get("definition_sql") or ""),
            output_columns=nq.get("output_columns") or (),
            shape=str(nq.get("shape") or "projection"),
            row_cap=nq.get("row_cap"),
            column_cap=nq.get("column_cap"),
            artifact=nq.get("artifact") if isinstance(nq.get("artifact"), dict) else None,
        )
    return result


async def load_named_queries(
    model_id: str,
    db: Any,
) -> dict[str, NamedQueryDefinition]:
    """Load Named Query definitions from the deployed snapshot, cached.

    An undeployed model has no snapshot -> no Named Queries defined (an
    unresolvable ``@name`` then gets the specific unknown-reference error).
    """
    from shared.db.models import Model, ModelVersion

    model = await db.get(Model, model_id)
    if model is None:
        return {}
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return {}
    deploy_epoch = getattr(model, "deploy_epoch", 0) or 0

    # Cache key carries the deploy EPOCH as well as the version id (Bug-9161
    # corrected Phase 1): the version gate elsewhere compares (version_id,
    # epoch) pairs, so a same-id/older-epoch pointer (a REVERT) must not keep
    # serving the definitions cached for the superseded deployment. Matches
    # the join-graph and snapshot caches, which key on the same triple.
    cache_key = (str(model_id), str(deployed_version_id), deploy_epoch)
    now = time.monotonic()
    cached = _NAMED_QUERY_CACHE.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    version = await db.get(ModelVersion, deployed_version_id)
    if version is None or not isinstance(version.snapshot_json, dict):
        return {}
    definitions = _extract_named_queries_from_snapshot(version.snapshot_json)
    if len(_NAMED_QUERY_CACHE) >= _MAX_CACHE_ENTRIES:
        oldest = min(_NAMED_QUERY_CACHE, key=lambda k: _NAMED_QUERY_CACHE[k][0])
        _NAMED_QUERY_CACHE.pop(oldest, None)
    _NAMED_QUERY_CACHE[cache_key] = (now + _CACHE_TTL_SECONDS, definitions)
    return definitions


# ---------------------------------------------------------------------------
# Reference-shape recognition (token-level, never text regex)
# ---------------------------------------------------------------------------

def _meaningful_tokens(sql: str, dialect: str = "postgres"):
    """Lex ``sql`` and drop tokens the shape check must ignore (semicolons,
    comments). Whitespace is already skipped by the sqlglot tokenizer."""
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    except Exception:  # pragma: no cover - lexer robustness
        return []
    return [
        t for t in tokens
        if t.token_type not in (TokenType.SEMICOLON, TokenType.COMMENT)
    ]


def named_query_reference_name(sql: str, dialect: str = "postgres") -> Optional[str]:
    """Return the referenced name when ``sql`` is a named-query-shaped
    reference, else None.

    The EXACT v1 shape (invariant 5): the whole statement is
    ``SELECT * FROM @name`` — no projection subset, no join, no WHERE, no
    decoration of any kind (a trailing semicolon and surrounding whitespace
    are tolerated). Recognition is token-level on the sqlglot lexer: an
    ``@name`` inside a string literal or comment is not a placeholder and
    never matches. This is the shape check the parameter-binding step uses to
    whitelist the placeholder AND the step-1.6 interceptor uses to dispatch.
    """
    meaningful = _meaningful_tokens(sql, dialect)
    # Expected token stream: SELECT, STAR, FROM, PARAMETER(@), VAR(name).
    if len(meaningful) != 5:
        return None
    if meaningful[0].token_type is not TokenType.SELECT:
        return None
    if meaningful[1].token_type is not TokenType.STAR:
        return None
    if meaningful[2].token_type is not TokenType.FROM:
        return None
    if meaningful[3].token_type is not TokenType.PARAMETER:
        return None
    if meaningful[4].token_type is not TokenType.VAR:
        return None
    return meaningful[4].text.strip()


def sql_references_named_query_position(sql: str) -> Optional[str]:
    """Return the ``@name`` in FROM position even for DECORATED shapes.

    Used to give the specific ``NQ_UNSUPPORTED_SHAPE`` error instead of the
    generic unknown-placeholder error: a query that places ``@name`` as the
    FROM target but is NOT the exact reference shape. Returns None when no
    ``@name`` sits in FROM position.
    """
    tokens = _meaningful_tokens(sql)
    for i, tok in enumerate(tokens):
        if tok.token_type is not TokenType.FROM:
            continue
        # The table reference is the token right after FROM (skipping
        # nothing — a parenthesised derived table or a real table name ends
        # the check; only an immediate PARAMETER+VAR pair is an @-reference).
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is None or nxt.token_type is not TokenType.PARAMETER:
            continue
        var = tokens[i + 2] if i + 2 < len(tokens) else None
        if var is not None and var.token_type is TokenType.VAR:
            return var.text.strip()
    return None


def _definition_is_row_preserving_projection(definition_sql: str) -> bool:
    """Structural half of the pocket row-preserving proof, on the definition.

    Mirrors pocket §5.1 rule 2: a row-preserving ``SELECT ... FROM <table>``
    with NO join, subquery, CTE, set operation, DISTINCT, GROUP BY, LIMIT,
    OFFSET, sample or table function. A projection-shaped Named Query that
    fails this is never served materialised to an RLS principal (live
    fallback). Returns True for an unparseable definition only when it is
    structurally a single select (fail closed on anything else).

    The predicate's SINGLE implementation now lives in
    ``shared/named_query/star_expansion.is_row_preserving_star_definition``
    (Bug-9161 corrected Phase 1), so the security proof here and the NQ
    star-expansion detection can never drift.
    """
    from shared.named_query.star_expansion import (
        is_row_preserving_star_definition,
    )

    return is_row_preserving_star_definition(definition_sql)


def manifest_has_security_columns(
    manifest: Optional[dict],
    security_columns: list[str],
    active_refresh_run_id: Any = None,
) -> bool:
    """True when every security column is a materialised OUTPUT column.

    The pocket §5.1 rule 3 verbatim, INCLUDING the liveness binding: the
    manifest must describe THIS artifact's live build (``build_refresh_run_id
    == active_refresh_run_id``, matching ``MANIFEST_VERSION``) or it proves
    nothing. Columns are matched CASE-SENSITIVELY against
    ``row_manifest.columns`` (``logical_name or physical_column``). The model
    shape is NOT a substitute — the manifest is the branch-independent record
    of what the build exposed. A missing/empty manifest fails closed.
    """
    if not isinstance(manifest, dict):
        return False
    if not security_columns:
        return False
    if not active_refresh_run_id:
        return False
    if str(manifest.get("build_refresh_run_id") or "") != str(
        active_refresh_run_id
    ):
        return False
    try:
        from shared.semantic.artifact_manifest import MANIFEST_VERSION
    except Exception:  # pragma: no cover - defensive
        return False
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return False
    columns = manifest.get("columns") or []
    materialised: set[str] = set()
    for col in columns:
        if not isinstance(col, dict):
            continue
        logical = col.get("logical_name")
        physical = col.get("physical_column")
        if logical:
            materialised.add(str(logical))
        if physical:
            materialised.add(str(physical))
    return all(col in materialised for col in security_columns)


def projection_security_proof_holds(
    *,
    definition_sql: str,
    manifest: Optional[dict],
    active_refresh_run_id: Any = None,
    security_columns: list[str],
    user_mapping_active: bool,
) -> bool:
    """The pocket §5.1 RLS proof, consumed verbatim for projection NQs.

    All of the following must hold (anything unproven -> live fallback):
    1. the compiled predicate names at least one security column and no
       ``user_mapping`` rule is active (checked by the caller, which has the
       compiled rules);
    2. the definition is a row-preserving ``SELECT * FROM <table>`` slice —
       no join/subquery/CTE/set-op/DISTINCT/GROUP BY/LIMIT/OFFSET;
    3. every security column is a materialised OUTPUT column of the manifest,
       matched case-sensitively.

    NOTE (documented divergence from the pocket route, fail-closed): a pocket
    additionally proves row-population equivalence (§5.0); a Named Query is
    served ONLY as ``SELECT *`` of its own definition, so there is no
    sub-projection population question — the materialised table IS the
    definition's result set.
    """
    if not security_columns:
        return False
    if user_mapping_active:
        return False
    if not _definition_is_row_preserving_projection(definition_sql):
        return False
    if not manifest_has_security_columns(
        manifest, security_columns,
        active_refresh_run_id=active_refresh_run_id,
    ):
        return False
    return True
