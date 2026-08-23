"""Shared database layer.

Importing ANY ``shared.db.*`` module installs the runtime model-write lock guard
(Bug-7982 R7, findings 3+4). This is the only hook guaranteed to run in every
process that touches the database — services, background jobs, scripts and test
harnesses alike — which is exactly the property the statically derived coverage
guard could not have: it enumerated writers by guessing where they live, and was
therefore blind by construction to non-route writers.

Installation only registers SQLAlchemy event listeners (no DB access, no ORM
import). Enforcement level is governed by
``settings.MODEL_WRITE_LOCK_GUARD_MODE`` and defaults to ``warn``.
"""
from shared.db.model_write_lock_guard import install as _install_model_write_guard

_install_model_write_guard()
