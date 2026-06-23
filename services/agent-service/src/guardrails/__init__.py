"""Guardrail layers — Phase D.

Three layers that wrap the answer pipeline:
  - input.py:  prompt-injection heuristics + denied-topic matching.
  - refuse.py: refusal taxonomy with suggested-rewording rendering.
  - output.py: brand voice template + banned-phrase + disclosure append.
  - block.py:  judge-block payload rendering (transparent / opaque).
"""
from src.guardrails.block import apply_judge_block, render_block_message
from src.guardrails.input import scan_input_message
from src.guardrails.output import apply_output_guardrails
from src.guardrails.refuse import REFUSAL_REASONS, render_refusal

__all__ = [
    "apply_judge_block",
    "render_block_message",
    "scan_input_message",
    "apply_output_guardrails",
    "REFUSAL_REASONS",
    "render_refusal",
]
