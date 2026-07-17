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

## Historical backtest — fastest path (free EOD parquet)

The [philippdubach/options-data](https://github.com/philippdubach/options-data)
dataset provides free EOD option chains for 100+ US equities/ETFs
(2008–2025) as one parquet per ticker, **including volume, open interest,
and implied volatility** — so the standard config and full liquidity filter
apply unchanged. Two commands to a multi-year backtest:

```bash
python scripts/import_parquet.py --tickers SPY QQQ IWM \
    --start 2024-01-01 --end 2026-06-30
python scripts/backtest.py --snapshots-dir data_snapshots_parquet
```

The importer also writes `settlements.json` from the dataset's own
underlying closes, so the backtest needs no network at all. Caveats: EOD
snapshots only (hold-to-expiry evaluation), community-sourced data
(spot-check a few chains against a broker), educational/research use per
the dataset's terms.

## DoltHub historical backtest (alternate free source)

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
