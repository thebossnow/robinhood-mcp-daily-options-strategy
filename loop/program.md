# Karpathy Autoresearch Loop for Strategy Improvement

This directory implements an autonomous loop for iteratively improving the trading strategy code. Inspired by Andrej Karpathy's AutoResearch and quant loop engineering best practices.

## Core Files
- program.md: Strategy bible + constraints
- evaluate.py: Objective verifier using backtests
- run_loop.py: Orchestrator
- state.json: Persistent experiment log

## Goal
Improve risk-adjusted performance (expectancy after costs, win rate, drawdown) on collected snapshots while strictly respecting all RiskManager limits and code safety.

## Constraints (non-negotiable)
- Allowed edits: options_trader/config.py (safe param ranges), options_trader/signals/ (filters, probability/EV logic)
- Forbidden: risk/, execution/, core paper_trade logic, AGENT_PROMPT.md (without review)
- Small atomic changes only
- Must pass full pytest
- Verifier decides acceptance

## Objective Function & Scoring
Primary composite: expectancy * win_rate / (1 + normalized max DD)
Stability (ICIR-inspired): mean performance / std across rolling windows or regimes
Persistence/decay: rolling performance trend or simple half-life proxy

Only accept if composite improves AND stability/persistence does not regress.

## Generation & Feedback
Propose multiple variants per round. Explicitly analyze past failures from state.

## OOS Gate
Use held-out snapshots or walk-forward. Raise bar with more attempts.

## Stop Conditions
Max iterations, no improvement streak, target score, or manual stop.

See README.md for full setup.