"""Ambient no-write fence for preview (dry-run) executions — Bug-9036.

A manual AI advisor run with ``dry_run=true`` created and materialised six
aggregates on a deployed optimizer. The dry-run flag WAS read — at the two
sites that happened to be on the reviewer's mind — and a third materialising
call in the same run body was not gated at all. That is the failure mode a
per-call-site ``if dry_run:`` always has: the guarantee lives in the CALLERS,
so it holds only for the callers someone remembered, and every new call site
re-opens the hole silently.

This module inverts the direction. The preview declares ITSELF, once, for its
whole body:

    with dry_run_write_fence(active=effective_dry_run):
        ...                      # the entire advisor run

and each WRITE PRIMITIVE asserts the fence before it writes:

    refuse_under_dry_run("create an aggregate")

The guarantee then belongs to the primitives. A future code path added inside
the preview body cannot write unless it also bypasses every write primitive the
platform has — which is separately forbidden (the source-execution rule in
CLAUDE.md requires all physical writes to go through
``shared/source_executor``). It is fail-closed: an un-audited new caller of a
guarded primitive is refused, not admitted.

Where the assertions live, and why THOSE places
------------------------------------------------
``shared/source_executor`` is the platform's declared physical-write boundary,
so every one of its PUBLIC write entry points asserts — not just the DDL one.
That distinction is the whole point: a fence placed only on
``execute_source_ddl`` would be structurally blind to a CROSS-DATABASE build,
which materialises through ``ensure_target_schema`` -> ``stream_to_staging_table``
-> ``bulk_insert_batched`` -> ``refresh_table_atomic_swap`` and issues no CTAS at
all. A guard whose discovery scope misses a whole class of the thing it guards
is the failure mode CLAUDE.md's coverage-blind-spot rule names, and it is
cheaper to close than to document.

Above that, the optimizer's own product-state primitives assert on their own
account, because they write ORM rows rather than physical storage:
``lifecycle.creator.create_aggregate``,
``lifecycle.creator.backfill_include_all_measures`` (which RETIRES and DROPs),
and ``ai/runner._create_aggregate_from_recommendation``.

Scope, stated so the fence is not read as a broader promise than it is. It
refuses PRODUCT-STATE writes: physical source writes of every shape, aggregate
definitions, columns, refresh policies, retirements and their lifecycle events.
It deliberately does NOT refuse the preview's own bookkeeping — the
``AIOptimizerRun`` row, its ``AIAggregateRecommendation`` preview rows, the
telemetry snapshot the preview reasons from, an alert recording a failed run,
or the ``models.target_id`` self-heal that runs before the preview can resolve
its own target. Those change neither what the query router serves nor what the
customer's source database stores, and refusing them would make the preview
fail instead of preview. Source READS are likewise untouched: a preview is
allowed to look.

Ambient by ``contextvars``, so it follows ``await`` within the run and is
inherited by any task the run spawns (``asyncio.create_task`` copies the
context) — inheritance in the fail-closed direction. ``active=False`` is a
no-op rather than a reset, so an inner scope can never clear an outer fence.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_FENCE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "tessallite_dry_run_write_fence", default=False
)


class DryRunWriteRefused(RuntimeError):
    """A product-state write was attempted inside a dry-run (preview) body.

    Raised rather than silently skipped: a preview that quietly swallowed a
    write attempt would hide the very defect this fence exists to make
    impossible, and the advisor's own error path records the message on the run
    row where an operator can see it.
    """


@contextmanager
def dry_run_write_fence(active: bool = True) -> Iterator[None]:
    """Mark the enclosed body as a preview that must not write product state.

    ``active=False`` leaves the ambient value untouched (it does NOT clear an
    outer fence), so nesting can only ever tighten the guarantee.
    """
    if not active:
        yield
        return
    token = _FENCE.set(True)
    try:
        yield
    finally:
        _FENCE.reset(token)


def enter_dry_run_write_fence(active: bool = True) -> object | None:
    """Imperative form of :func:`dry_run_write_fence`; pair with :func:`exit_`.

    Exists for one caller: ``ai/runner.run_ai_optimizer``, whose preview flag is
    only known part-way down a long body that already has its own
    ``try/except``. Wrapping that body in a ``with`` would re-indent several
    hundred lines of guarded logic for no behavioural gain, and a large
    mechanical re-indent across a fail-closed run path is a worse risk than a
    two-function imperative pair. Returns an opaque token (``None`` when
    inactive) that :func:`exit_dry_run_write_fence` restores.
    """
    return _FENCE.set(True) if active else None


def exit_dry_run_write_fence(token: object | None) -> None:
    """Restore the fence state captured by :func:`enter_dry_run_write_fence`."""
    if token is not None:
        _FENCE.reset(token)  # type: ignore[arg-type]


def in_dry_run_write_fence() -> bool:
    """True while the current execution context is a preview."""
    return _FENCE.get()


def refuse_under_dry_run(operation: str) -> None:
    """Refuse *operation* when it is reached inside a preview body.

    Call at the TOP of a write primitive, before any row is added, any lock is
    taken and any DDL is issued — the refusal must leave nothing behind.
    """
    if _FENCE.get():
        raise DryRunWriteRefused(
            f"Refused to {operation} during a dry run. A dry run previews what "
            "would be built; it must not create, materialise, retire or drop "
            "anything. This is a fail-closed guard: reaching it means a write "
            "path inside the preview body was not gated on the dry-run flag."
        )
