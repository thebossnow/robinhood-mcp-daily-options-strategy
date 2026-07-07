# robinhood-mcp-daily-options-strategy

AI-assisted daily options strategy for the Robinhood Agentic Trading MCP.
Screens liquid underlyings (SPY/QQQ/IWM) for single-leg, defined-risk debit
trades that pass **liquidity, positive-expectancy, and position-sizing** rules,
with mandatory journaling so the strategy's edge (or lack of one) is measured,
not assumed.

## Honest framing

Buying cheap short-dated options is negative-expectancy by default: they are
cheap because the market prices their odds as poor, and bid/ask spread drag is
brutal at low premiums. This repo's approach is therefore *rejection-first* —
the scanner's job is to say **NO QUALIFYING TRADE TODAY** unless a trade's
implied odds actually support the target, and the journal exists to prove or
disprove the edge within ~30 trades. Do not scale size until the journal shows
positive expectancy.

## What's here

| Path | Purpose |
|---|---|
| `AGENT_PROMPT.md` | Paste-in prompt for the MCP-connected agent: preflight, regime filter, criteria, sizing, execution workflow. |
| `scripts/strategy_math.py` | Pure, unit-tested math: mid/spread, Black-Scholes P(ITM)/P(touch), expected move, EV, sizing. |
| `scripts/options_scanner.py` | Delayed-data (yfinance) pre-screen producing ranked `TradePlan`s. The agent re-verifies with live MCP quotes. |
| `scripts/journal.py` | Trade journal CLI: log entries (and no-trade days), record exits, report win rate & expectancy. |
| `tests/` | Unit tests for the strategy math (`python -m unittest discover tests`). |

## Setup

```bash
pip install -r requirements.txt
python -m unittest discover tests          # verify the math
python scripts/options_scanner.py --account-value 500
```

## Trade rules (enforced in code)

- **Structure:** single-leg long call/put only — the MCP supports nothing else.
  (Debit spreads are the better structure; place those manually in the app.)
- **Premium:** $0.30–$5.00 mid. **Spread:** ≤10% of mid. **Liquidity:** OI ≥500
  or volume ≥100 on the exact contract.
- **Strike:** within 1 expected move (0.85 × ATM straddle) of spot.
- **Expectancy:** P(touch)-based EV must be positive at +100% target / −50% stop.
- **Sizing:** risk ≤2% of account per trade; 1 contract until 30 journaled trades;
  −4% daily loss cap halts new entries.
- **Exits:** +100% target, −50% stop, or 15:45 ET time stop — whichever hits first.

## Account requirements

The Robinhood account must be **agentic-enabled** (`agentic_allowed=true` via
`get_accounts`) and have **option level 2+**. Margin/level upgrades are done in
the Robinhood app/website, not via the MCP.

## Workflow

1. Agent runs the preflight + regime filter from `AGENT_PROMPT.md`.
2. Pre-screen with `options_scanner.py`; re-verify candidates with live MCP quotes.
3. `review_option_order` → user confirms → `place_option_order`.
4. Log everything with `journal.py` (including no-trade days).
5. Weekly: `journal.py stats`. Negative expectancy after 30 closed trades means
   stop and rework, not resize.

## Disclaimer

Not financial advice. Options involve substantial risk of loss. This is a
research/automation project; every order requires explicit human confirmation.
