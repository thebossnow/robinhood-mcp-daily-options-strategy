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
