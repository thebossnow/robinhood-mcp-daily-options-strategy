"""Agentic options trading pipeline.

Deterministic code computes everything numeric (filters, expected value,
sizing, risk limits). An LLM agent sits on top for regime/catalyst judgment
and narrative only — numbers come from code, never from the model.
"""

__version__ = "0.2.0"
