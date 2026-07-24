# Autoresearch Loop — Design Document

**Status: DESIGN + VERIFIER ONLY.** `evaluate.py` (the verifier) is
implemented and unit-tested. `run_loop.py` (the proposal/orchestration step)
is a stub. Nothing here runs autonomously yet, and nothing should until the
data gates below are met.

The idea: a Karpathy-style propose→verify iteration loop where an LLM
proposes small changes to the strategy's *signal* logic, and a deterministic
verifier decides acceptance from backtest evidence. The LLM never grades its
own work.

## Edit scope

**Allowed:**
- `options_trader/signals/` — filters, probability/EV logic.
- `StrategyConfig` signal parameters only: liquidity thresholds
  (`min_open_interest`, `min_volume`, `max_spread_pct`), structure
  (`spread_widths`, `max_debit_fraction`, `min_debit`), expectancy knobs
  (`min_p_win`, `min_ev_after_costs`, `slippage_half_spread_frac`), and the
  DTE window.

**Frozen — verifier rejects any variant touching these:**
- `account_equity`, `max_risk_per_trade_pct`, `daily_loss_limit_pct`,
  `max_open_positions`, `max_consecutive_losses` (see
  `FROZEN_RISK_FIELDS` in `evaluate.py`). An earlier draft of this document
  listed `config.py` as broadly editable; that was a contradiction — the
  kill switch and loss limits live in `config.py`. Only the whitelisted
  signal fields are in scope.
- `options_trader/risk/`, `options_trader/execution/`,
  `options_trader/journal/`, `scripts/paper_trade.py`, `AGENT_PROMPT.md`.

Every variant must be a small atomic diff and pass the full pytest suite
before it even reaches the verifier.

## Objective

Primary metric: **expectancy per trade after costs** (dollars), from the
snapshot-replay backtest. Constraints, not multiplied into a composite
(win-rate multipliers reward negative-skew strategies and double-count what
expectancy already contains):

- Max drawdown must stay within an absolute dollar limit and must not
  regress more than 25% vs. baseline.
- Minimum sample: ≥ 40 settled trades in-sample.

## Statistical discipline

- **Data gate:** no evaluation until snapshots span ≥ 30 distinct scan days.
  With 45-minute intraday collection this accumulates quickly, but intraday
  snapshots of the same chain are highly correlated — the verifier
  deduplicates to one snapshot per day per underlying/expiration before
  computing statistics.
- **Walk-forward OOS:** the most recent ~30% of scan days are held out.
  Accept only if the variant improves in-sample AND does not regress
  out-of-sample (and OOS expectancy is positive).
- **Escalating bar:** each prior rejected attempt raises the required
  in-sample improvement margin (multiple-comparisons guard). The OOS set is
  a finite resource; after ~10 attempts, stop and collect more data instead
  of continuing to mine.
- **OOS calibration pairing:** once a candidate has ≥ 40 OOS settled trades,
  its predicted `ev_after_costs` must have a positive OLS slope against its
  own OOS P&L (`options_trader/backtest/calibration.py::ev_calibration`,
  wired into `decide()`). Beating baseline on average isn't enough if the
  "edge" doesn't track realized outcomes — that's a model artifact, not a
  signal improvement.

## Live anchor (beyond backtest data)

The verifier above only ever sees backtest replay. Two more pieces close the
loop against what actually happens in the account, i.e. the "real fills"
anchor that no backtest can substitute for:

- `scripts/calibrate_live.py` — reports predicted EV vs. realized P&L over
  the journal's *closed* trades (paper or live), optionally diffed against a
  backtest's predicted expectancy (`--backtest-trades`). Run it by hand
  whenever you want the honest number: is the backtest's edge showing up in
  real fills?
- `loop/audit_live.py` — the slow loop watching the fast one. Run
  periodically (weekly); if live EV-calibration slope collapses (≤ 0) or
  mean realized P&L turns non-positive over ≥ 40 live trades, it writes
  `loop/live_halt.json` and every `RiskManager.check()` call refuses new
  entries until a human runs `--clear` after reviewing why. This is separate
  from the consecutive-loss kill switch: that catches a losing streak, this
  catches the model's predictions quietly decoupling from reality while the
  streak still looks fine.

## Process

1. Proposal step (not yet implemented) generates a variant diff + rationale.
2. CI: full pytest must pass.
3. Verifier: `evaluate.py` renders an accept/reject with reasons, logged to
   `loop/state.json` (append-only experiment log, includes rejected
   attempts — failures are feedback for the next proposal).
4. **Accepted variants become pull requests for human review. The loop never
   merges, never touches live/paper execution, and never edits its own
   verifier or this document.**

## Stop conditions

Max attempts per data vintage (~10), no-improvement streak (3), or manual
stop. The loop halts entirely if the paper-trading kill switch is active.
