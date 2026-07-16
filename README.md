# Agentic Daily Options Trader (Robinhood MCP)

A defined-risk vertical-spread trading pipeline designed to be driven by an
MCP-connected AI agent. Deterministic Python computes everything numeric —
liquidity filters, probability-weighted expected value, position sizing, and
hard risk limits. The agent's job is narrowed to judgment and narrative:
regime/catalyst context, explaining a trade, and asking the human for
confirmation. **Numbers come from code, never from the model.**

> **This project makes no profitability claim.** Profitability is a
> hypothesis you validate with data, not a feature you install. The pipeline
> is built so that the strategy must *prove itself* — first in backtests over
> collected snapshots, then in a paper-trading period — before a single live
> order is placed. Options trading involves substantial risk of loss. Nothing
> here is financial advice.

## Why this replaced the original "cheap OTM options" strategy

The first version of this repo filtered for options under $20/contract with a
"2x target." That criterion mathematically forces far-OTM near-expiry
contracts (a $0.20 option on SPY), which lose with high probability, and its
risk/reward check was a tautology — `rr = (2 × premium) / premium` is always
exactly 2. The rewrite keeps the good instincts (strict liquidity, small
size, journaling, "NO TRADE" as a first-class answer) and replaces the core:

**PR#3 synthesis additions (adapted cleanly):**
- Shared pure math (expected move from straddle, prob-touch, premium-band liquidity).
- MCP-aware agent prompt (account preflight, event regime filters, single-leg
  reality note while preferring verticals).
- Explicit no-trade logging + 30-trade statistical significance rule.
- Expected-move strike filtering to avoid unrealistic targets.

| | Before | Now |
|---|---|---|
| Structure | single-leg cheap OTM | vertical debit spreads (defined risk) |
| Risk/reward | asserted ("target = 2x") | Black-Scholes probability-weighted EV per spread |
| Costs | ignored | slippage charged on entry *and* exit; EV must survive it |
| Liquidity | OI **or** volume (bug) | OI **and** volume **and** ≤10% spread **and** live bid, per leg |
| Risk limits | prompt text | enforced in code: per-trade cap, daily loss limit, position cap, kill switch |
| Validation | none | snapshot-replay backtest + paper journal with stats |

## Architecture

```
options_trader/
  config.py       StrategyConfig — every knob in one place
  data/           DataProvider interface, yfinance fallback, SnapshotStore
  signals/        Black-Scholes probabilities + vertical spread EV scoring
  risk/           RiskManager: sizing, daily loss limit, kill switch
  execution/      PaperBroker (pessimistic fills); template for MCP executor
  journal/        SQLite journal: every entry/exit + filter values at entry
  backtest/       replay stored snapshots to expiry settlement
scripts/
  scan.py         daily scan → report + runs/scan_*.json
  paper_trade.py  open / close / settle / status / stats
  backtest.py     replay data_snapshots/ once expiries have settled
```

The `DataProvider` interface is the MCP seam: when Robinhood MCP options
tools are available, implement the same three methods (`get_spot`,
`get_expirations`, `get_chain`) against MCP and everything downstream —
filters, EV, risk, journal — works unchanged.

## How a candidate qualifies

Every vertical (bull call / bear put, configurable widths) must pass **all**:

1. **Liquidity, per leg** — bid > 0, OI ≥ 500, volume ≥ 50, bid/ask spread
   ≤ 10% of mid.
2. **Structure** — net debit ≤ 45% of width (max profit comfortably exceeds
   max loss) and ≥ $0.10.
3. **Probability** — P(max profit) ≥ 25%, from N(d2) at each leg's implied
   vol. A model estimate, not truth — see `signals/probability.py`.
4. **Expectancy after costs** — probability-weighted EV, minus slippage
   charged at half of each leg's half-spread on entry *and* exit, must be
   positive. If nothing passes, the answer is `NO QUALIFYING TRADE TODAY`.

## Hard risk limits (code, not prompt)

`RiskManager` refuses any trade that would breach: max loss > 1% of equity
per trade, realized daily loss past 2% of equity, more than 3 open positions,
3 consecutive losses (kill switch — requires human review to resume), or
total open risk past a portfolio heat cap. The paper broker (and any future
live executor) cannot open a position without a passing check.

## Daily workflow

```bash
pip install -r requirements.txt

# 1. Scan (also archives chains for the backtest dataset)
python scripts/scan.py --save-snapshot

# 2. Open a paper position from the scan output (risk-checked, slippage applied)
python scripts/paper_trade.py open --scan-file runs/scan_<ts>.json --index 0

# 3. Manage
python scripts/paper_trade.py status
python scripts/paper_trade.py close --id 3 --value 1.35   # close at current spread mid
python scripts/paper_trade.py settle                       # settle past-expiry positions

# 4. Review
python scripts/paper_trade.py stats
python scripts/backtest.py        # replay collected snapshots at settlement
```

Run tests with `python -m pytest tests/`.

## Backtesting honestly

Free historical option-chain data doesn't exist, so `scan.py --save-snapshot`
builds the dataset forward: point-in-time chains with no survivorship or
revision bias. `backtest.py` replays them with the same code path as the
scanner — entry at mid + slippage, held to expiry, settled at intrinsic (the
most conservative management assumption). Unsettled expiries are skipped,
never guessed. Paid alternatives (ORATS, CBOE DataShop) can bootstrap a
longer history behind the same `ChainSnapshot` format.

## Gate before any live trading

Do not wire live execution until **all** of:

- ≥ 60 days of collected snapshots and a backtest with positive expectancy
  after costs;
- ≥ 40 closed paper trades with positive expectancy (`paper_trade.py stats`);
- max drawdown in paper within what you'd tolerate live;
- a human-confirmation step on every order (the agent proposes, you approve).

Also know the frictions this size of account faces: pattern day trader rules
under $25k on margin, short-term capital gains tax on every win, and spreads/
slippage that compound daily. These are why the EV filter charges costs
up front.

## Agent integration

`AGENT_PROMPT.md` contains the prompt for an MCP-connected agent. It is a
*tool contract*: the agent runs the pipeline and reports its numbers; it may
veto trades on qualitative grounds (event risk, regime) but may never
override a risk refusal or replace computed numbers with its own.

## Autoresearch loop (design stage)

`loop/` holds the design for a propose→verify improvement loop over the
strategy's *signal* parameters: `loop/program.md` is the design doc and
`loop/evaluate.py` is the verifier. Current status, honestly stated: the
verifier is implemented and unit-tested; the proposal/orchestration step
(`loop/run_loop.py`) is a stub and does not run autonomously.

Non-negotiable properties, enforced in code rather than prose:

- **Risk limits are frozen.** The verifier rejects any variant that changes
  `account_equity`, `max_risk_per_trade_pct`, `daily_loss_limit_pct`,
  `max_open_positions`, or `max_consecutive_losses`.
- **Data gates.** No evaluation until the snapshot dataset spans ≥30 distinct
  scan days and the baseline backtest settles ≥40 trades. Intraday snapshots
  are deduplicated to one per day per underlying/expiration so correlated
  samples don't inflate the statistics.
- **Walk-forward out-of-sample.** The most recent ~30% of scan days are held
  out; a variant must improve in-sample *and* not regress out-of-sample.
- **Escalating acceptance bar.** Each rejected attempt raises the required
  improvement margin, as a guard against multiple-comparisons data mining.
- **Human merge.** An accepted variant becomes a pull request for review.
  Nothing the loop produces merges automatically.

## DoltHub historical backtest (free EOD data)

The free [post-no-preference/options](https://www.dolthub.com/repositories/post-no-preference/options)
DoltHub database has end-of-day US equity option chains back to ~2019.
`scripts/import_dolthub.py` converts it into this repo's snapshot format so
the same backtest and verifier code runs over years of history immediately,
instead of waiting for forward collection to accumulate:

```bash
python scripts/import_dolthub.py --symbols SPY QQQ IWM \
    --start 2024-01-01 --end 2026-06-30
python scripts/backtest.py --snapshots-dir data_snapshots_dolthub \
    --config configs/dolthub_backtest.json
```

Read results with these caveats in mind:

- **The dataset has no volume or open-interest columns.** Imported rows
  carry 0 for both, and `configs/dolthub_backtest.json` zeroes those two
  liquidity minimums so candidates can form at all. Every other filter
  (live bid, ≤10% spread, structure, EV after costs) still applies — but
  results are **optimistic on liquidity**: some historical "trades" may
  have been practically unfillable.
- EOD snapshots only (stamped 16:00) — no intraday management can be
  evaluated, matching the engine's hold-to-expiry assumption.
- Community-maintained data: spot-check a few chains against a broker.
- Imports live in `data_snapshots_dolthub/`, deliberately separate from the
  live 45-minute collection in `data_snapshots/` — the forward-collected
  set has real volume/OI and intraday quotes and remains the
  higher-fidelity benchmark. Agreement between the two is the signal that
  matters.

## Credit strategies: put spreads and iron condors (premium selling)

The debit-spread scanner above selects by a model-EV filter that scores the
market's own prices with the market's own implied vol — a construction that
can only surface model error, not edge (risk-neutral EV of a fairly priced
structure is ~0 before costs). The credit-strategy pipeline replaces that
premise: mechanical premium *selling*, whose edge claim (the variance risk
premium) is tested against history instead of asserted by a model.

Three variants (`options_trader/signals/credit.py`):

| variant | put side | call side | idea |
|---|---|---|---|
| `put_spread` | short ~30Δ + wing | — | bullish lean + VRP |
| `condor_sym` | short ~20Δ + wing | short ~20Δ + wing | pure range bet |
| `condor_asym` | short ~20Δ + wing | short ~12Δ + wing | more room on the side bull runs punish |

Shared mechanics: 25–50 DTE entries targeting 45 (the classic 30–45 window
falls between this dataset's rotating ~{11–16, 25–32, 46} DTE quote
buckets), wings ~2% of spot, weekly entry cadence, managed mechanically —
close at 50% of entry credit, exit on short-strike breach, time exit at
min(21 DTE, half the entry DTE). No rolling, no discretion. Fills pay
slippage (half-spread fraction) both ways.

```bash
python scripts/backtest_credit.py --symbols SPY QQQ IWM XLF XLE \
    --start 2022-01-03 --end 2026-06-30
```

The backtest (`options_trader/backtest/managed.py`) replays weekly
checkpoints against DoltHub EOD chains fetched lazily with an on-disk cache
(`data_dolthub_cache/`). Additional caveats on top of the DoltHub ones
above: the dataset quotes only a rotating subset of expirations per day, so
positions whose expiration is unquoted at a checkpoint are marked with
Black-Scholes at entry IV — each trade records `mark_source` and the
summary reports how much of the result rests on model marks. Weekly (not
daily) management means late profit-takes (pessimistic) and late breach
exits (also pessimistic for a short-premium book). It is an expectancy
study, not a portfolio simulation: one contract per position, overlapping
positions are correlated, and trade counts overstate statistical
independence.

### First results (2022-01-03 → 2026-06-30, SPY/XLF/XLE): no edge found

The dataset does not cover QQQ or IWM (verified directly — it has DIA,
SPY, XLE, XLF among liquid ETFs), so the run is SPY/XLF/XLE, 976 trades.
All three variants were **negative after costs** (expectancy −$47 to −$62
per contract, profit factor ≈ 0.4, win rates 33–38%). Decomposition:

- **Breach exits are the dominant cost**: 46% of trades stopped out on a
  short-strike touch, all losers, −$160 average. Touch probability at
  these deltas is ~2× the expiry ITM probability, so stop-on-touch
  converts a majority-winner structure into a majority-loser one. With
  breach exits off, expectancy improves to ≈ −$39/trade — still negative.
- **Pre-cost, the strategy is ≈ breakeven only on SPY** (condor_sym
  −$1.5/trade with zero slippage); XLF is mildly negative; XLE is ruinous
  (condor win rate 2% — the 2022 energy trend walked straight through the
  "rangebound" range). Slippage adds ~$10–25/trade of drag.
- 78% of exits are model-marked (see `mark_source`), so treat magnitudes
  as approximate; the sign and the breach-exit decomposition are robust
  across the marks-only subset.

Read this the way the repo reads "NO QUALIFYING TRADE": a valid, useful
outcome. The mechanics most often quoted in retail playbooks did not
survive contact with 4.5 years of data on these underlyings under this
fill model.

### Validated variants and the sweep that produced them

A 16-config sweep (deltas 10–30, wings 2%/4%, breach on/off; fit on
2022–24, validated out-of-sample 2025–26) measured each lever:
farther-OTM shorts help monotonically, removing the breach stop helps
everywhere, SPY-only is the biggest lever (~+$40/trade vs including
XLF/XLE), and an IV-rank ≥ 50 entry filter — the playbook's favorite —
made results sharply worse (high-IV weeks cluster with the trends that
breach condors). Two configs survived both halves, shipped as
`VALIDATED` in `signals/credit.py`:

| variant | structure | 2022–26 SPY backtest |
|---|---|---|
| `spy_condor15` | 15Δ condor, 4% wings, no breach stop | +$5.9/trade, 68% win, PF 1.06 |
| `spy_put10` | 10Δ put spread, 2% wing, no breach stop | +$5.5/trade, 81% win, PF 1.21 |

Those magnitudes are statistically indistinguishable from breakeven
(t < 1, and best-of-16 selection bias applies) — which is exactly why the
next gate is live paper trading, not live money.

### Paper trading the validated variants

```bash
python scripts/scan_credit.py --provider mcp          # entries, Mondays
python scripts/manage_credit.py --provider mcp        # daily management
python scripts/backtest_credit.py --start ... --end ...   # validated set by default
```

Cron (in `crontab.txt`): entries Mondays 10:30 ET, management daily
15:45 ET. Entries journal every skip as NO QUALIFYING TRADE; management
applies 50% profit-take (daily — the fidelity upgrade over the weekly
backtest), a time exit at min(21 DTE, half entry DTE), and settlement.
The journal gained additive `strategy`/`legs_json` columns; existing
vertical rows and code are untouched.

**Sizing:** `configs/credit_paper.json` runs a NOTIONAL $50k account —
the validated structures risk ~$1.2–2.4k per contract, which no $5k
account can carry (options: XSP at 1/10th scale, or more capital; that
decision is deliberately deferred). The paper phase measures what 26
trades/config/6-months actually can: realized slippage vs the 0.5×
half-spread assumption, daily-vs-weekly management uplift, and
no-disaster confirmation — not statistical proof of a +$6/trade edge.

## Calibration study: do the model's numbers predict reality?

Every candidate carries model outputs — `p_win`/`p_loss` from N(d2) at
market implied vol, and probability-weighted `ev_after_costs`. Those come
from the market's own risk-neutral pricing plus a crude middle-region
approximation, so a positive predicted EV can be real edge (skew harvested
between legs) or pure model artifact. Before trusting the EV ranking — and
before any sizing upgrade that would consume it (e.g. edge-proportional
fractional-Kelly sizing under the fixed 1% cap) — check calibration against
settled outcomes:

```bash
python scripts/backtest.py --snapshots-dir data_snapshots_dolthub \
    --config configs/dolthub_backtest.json \
    --save-trades runs/dolthub_trades.json
python scripts/calibrate.py runs/dolthub_trades.json           # pooled
python scripts/calibrate.py runs/dolthub_trades.json --by kind # per-slice
```

The report (`options_trader/backtest/calibration.py`) shows reliability
tables (predicted probability vs. realized frequency, with Wilson
intervals) for both the win and loss tails, Brier skill vs. a base-rate
predictor, and predicted-EV-vs-realized-P&L by quantile with the OLS slope.
Reading it: probabilities inside their intervals and an EV slope near 1
mean the signal is informative; skill ≤ 0 or a slope near 0 means the
surfaced "edge" doesn't cash — fix the signals before building anything on
top of them. The report prints its own caveats: same-day trades across
correlated underlyings are not independent samples, and DoltHub results
are optimistic on liquidity.
