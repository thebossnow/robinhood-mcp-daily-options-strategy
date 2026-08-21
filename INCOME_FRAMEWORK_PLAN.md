# Income Framework: Plan, Status, and Open Work

Companion to `INCOME_AGENT_PROMPT.md` (the drop-in prompt) and the README's
"Income framework" section (the findings). This file is the working plan:
what was built, what was deliberately not built, and what has to happen
before any of it is trusted.

## Goal

Take a prose premium-selling framework — cash-secured puts and iron
condors, 20–30Δ shorts, 30–45 DTE, IV rank > 30, ⅓-of-width condor credit,
50% profit take, roll-or-close on an IV spike, 10/7/5% sizing tiers — and
make it (a) runnable, (b) testable, and (c) checked against what this
repository has already measured, rather than adopted on the strength of how
familiar it sounds.

## Design decision: ship both readings

The framework and the repo's measurements disagree on four parameters. The
tempting moves are both wrong:

- **Implement it verbatim** and inherit four parameters this repo has
  evidence against.
- **Silently "fix" it** and hand back something that is no longer the thing
  that was asked for, with the disagreement buried in a commit message.

So `options_trader/signals/income.py` ships two profiles. `as_specified` is
verbatim and runnable — which is what makes its refusals *observable*
instead of arguable. `evidence_adjusted` is the default and changes only
parameters with a measurement behind them. Every difference is annotated in
the module docstring with the specific finding it rests on.

## Status

| area | module | state |
|---|---|---|
| tiered sizing + feasibility | `risk/sizing.py` | done, 35 tests |
| IV rank / percentile | `signals/iv_rank.py` | done, 21 tests |
| cash-secured puts | `signals/csp.py` | done, 15 tests |
| event blackout + span | `risk/events.py` | done, 23 tests |
| graded IV-spike response | `risk/iv_spike.py` | done, 16 tests |
| trade report contract | `reporting/trade_report.py` | done, 16 tests |
| profiles + cadence | `signals/income.py` | done, 29 tests |
| entry scanner | `scripts/scan_income.py` | done, end-to-end tested |
| daily management | `scripts/manage_income.py` | done |
| feasibility CLI | `scripts/size_check.py` | done |
| cron wiring | `scripts/income_*.sh`, `crontab.txt` | done |
| **backtest of the two profiles** | — | **not done, see below** |

Total: 476 tests pass (272 pre-existing, 204 new).

Three bugs were found by review after the first push and fixed:

1. **Both books shared one query.** Income entries were journaled with
   `strategy='credit'`, so `open_credit_positions()` handed them to
   `manage_credit.py` — which would manage them at 12:45 with `VALIDATED`
   parameters (or bare defaults) before `manage_income.py` saw them at
   12:50. Entries are now tagged `income` and each script reads its own
   book. The trap in that fix: `record_exit` flips the P&L sign only for
   credit-like strategies, so a new tag missing from `CREDIT_STRATEGIES`
   would have inverted every income P&L silently. It is in the set, and a
   test pins it.
2. **The IV/delta confirmation was decorative.** Neither production
   provider publishes a `delta` column, so every live report read
   `NOT CONFIRMED` — and the scanner opened the position anyway,
   contradicting its own documented rule. Two changes: `build_position`
   now records the Black-Scholes delta it already computes for strike
   selection, labelled `model` vs `chain`, and the scanner refuses any
   candidate whose confirmation fails. The earlier design treated a model
   delta as *not* a confirmation, which made the gate unsatisfiable on
   every real chain — the same unsatisfiable-rule failure this project
   criticized in the framework's event rule, committed in our own code.
3. **The IV-spike grading could never escalate.** Same root cause: with no
   delta available, `assess()` could only ever reach WATCH, so DEFEND and
   CLOSE were unreachable outside tests. The short leg's delta is now
   modelled from its live IV when the chain omits it.

One pre-existing bug was also found on the way:
`scripts/manage_credit.py`'s `manage_position` resolved variant parameters
from `VALIDATED` only, so any variant outside that registry silently
inherited `CreditVariantConfig`'s defaults — a 4-DTE weekly would take the
21-DTE time exit and close a day early. It now accepts an explicit `vcfg`,
and `tests/test_income_scripts.py::TestExplicitVariantOverride` pins the
behavior in both directions.

## Not done: the head-to-head backtest

The obvious next question — *does `evidence_adjusted` actually beat
`as_specified` on the same data?* — was not answered here, and the reason
is environmental, not analytical. The session that built this had no
options data available:

- yfinance and DoltHub are outside the sandbox's network allowlist.
- The Alpha Vantage key reachable via MCP has no options entitlement:
  `HISTORICAL_OPTIONS` and `REALTIME_OPTIONS` both refuse. `TIME_SERIES_DAILY`
  works, which is where the SPY 762.60 / 2026-08-20 close quoted in the
  README and prompt comes from.

Every number in the docs is therefore either (a) a measurement already in
this repo's README from the 2022–26 sweep, (b) arithmetic on that real SPY
close, or (c) explicitly labelled synthetic. **No option price in the docs
is invented.** The illustrative event dates in `configs/events_EXAMPLE.json`
are labelled `ILLUSTRATIVE — verify` in the data itself, and the real
template ships empty.

To close the gap, on a machine with DoltHub reachable:

```bash
# 1. Extend the sweep to the income profiles' geometry
python scripts/sweep_put_credit.py --symbols SPY --start 2022-01-03 --end 2026-06-30

# 2. Run each profile's variants through the managed backtest
python scripts/backtest_credit.py --symbols SPY --start 2022-01-03 --end 2026-06-30
```

The three questions worth the data, in order of how much they would change
the shipped defaults:

1. **Is IV rank > 30 helpful, harmful, or noise?** The sweep tested a hard
   IVR ≥ 50 filter and found it harmful. 30 is untested. This is the only
   parameter currently shipped as "record but do not act", and it is the
   cheapest one to settle — the data is the same data.
2. **Does the graded IV-spike response beat holding?** WATCH/DEFEND/CLOSE
   is currently reasoned from the breach-stop result rather than measured
   in its own right. It is report-only in `manage_income.py` precisely
   because of that. Measuring it needs intraday or at least daily IV
   series, which the weekly-checkpoint backtest does not currently carry.
3. **Does the 30–45 DTE window cost anything?** The validated variants were
   fit at 25–50 DTE targeting 45; the income profiles narrow that to 30–45
   targeting 40 to honor the framework. The DoltHub dataset's rotating
   expiration buckets are exactly why the original window was widened, so
   this narrowing may reduce the number of tradeable weeks.

## Open questions for the operator

1. **Capitalization.** `configs/income_framework.json` runs a notional
   $75,000 because that is what the default profile's 4%-wing condor needs
   at the 5% tier. If the real account is smaller, the choice is narrower
   wings (untested geometry), a cheaper underlying (XSP), or fewer
   structures — not a smaller tier percentage.
2. **The event calendar is empty.** Nothing gates on FOMC or CPI until
   `configs/events.json` exists with real dates. The scanner says so on
   every run rather than implying the calendar was clean.
3. **Cash-secured puts.** Disabled by default on the index for the reason
   in the README. Re-enabling for a cheaper underlying needs
   `with_underlying_scale()` so the wings land on real strikes, plus a
   decision on the assignment plan (`wheel` vs `close`).
4. **`typical_event_move_pct` is a placeholder.** `premium_compensates()`
   defaults to 1% of spot per macro event. The honest version is measured
   from realized absolute moves on past FOMC and CPI days for the traded
   underlying. Until then, treat a failing premium test as a caution, not
   a verdict.

## What would make this trustworthy

Nothing here establishes an edge, and `evidence_adjusted` inherits its
parents' caveats rather than escaping them: t < 1 on the sweep's
expectancy, best-of-16 selection bias, 78% model-marked exits, and a DTE
window narrower than the one that was fit. The order of operations is
unchanged from the rest of the repo — backtest, then paper, then the live
gate in README.md — and the income framework has completed none of the
three.
