"""Eval harness — Phase C3.

Iterates the union of every allow-listed model's ``example_questions``
for a project, runs each through the live ``run_turn`` pipeline, and
emits a structured report. Used by CI smoke-tests and the per-project
"Run eval" admin button.
"""
from src.eval.runner import EvalReport, EvalRow, run_eval_for_project

__all__ = ["EvalReport", "EvalRow", "run_eval_for_project"]
