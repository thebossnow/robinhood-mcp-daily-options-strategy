# Small-Account Options Income Backtest Spec

**Purpose:** Give an AI coding/research agent everything needed to backtest at least **3 defined-risk, income-first options strategies** on a small account. Prefer **consistency and high win-rate** over large home runs. Smaller wins are acceptable.

**Not financial advice.** This is a research protocol. Real trading involves assignment risk, gaps, early exercise, dividends, borrow fees, and data errors.

---

## 1. Account and design constraints

| Parameter | Default | Notes |
|---|---|---|
| Starting capital | `$10,000` | Also run `$5,000` and `$25,000` as sensitivity |
| Max risk per trade | `2%` of equity | Defined-risk structures only |
| Max concurrent trades | `4` | Avoid stacking correlated miners/memes |
| Max allocation to one ticker | `25%` of equity | Includes collateral + spread margin |
| Commission | `$0.65` per contract per side | Also test `$0.00` and `$1.00` |
| Slippage | Mid + `$0.02` per option, or 10% of mid if wider | Never assume fills at mid on AMC/SNAP far OTM |
| No naked short options | Required | Always buy the wing |
| No 0DTE as core strategy | Required | Too noisy for small accounts |
| Prefer weeklies / 7–45 DTE | Required | Income + faster compounding |

**Goal function (in order):**
1. Consistency: high % winning months, low max drawdown
2. Positive expectancy after costs
3. Capital efficiency (return on allocated capital)
4. Absolute P&L is secondary

A strategy that makes `$80–$200` per week with shallow drawdowns beats one that makes `$2,000` then gives it back.

---

## 2. Ticker universe

Use this exact universe unless a ticker has no usable options history. Split into **core** (more consistent) and **satellite** (higher IV, smaller size).

### Core (priority for consistency)
| Ticker | Role | Why it is here |
|---|---|---|
| `F` | Core income | Cheap shares, deep weeklies, dividend, tight markets |
| `SOFI` | Core income | High option volume, still low share price |
| `AAL` | Core / cyclical | Liquid airline, lower price, frequent option activity |
| `RIVN` | Core / growth-lite | Liquid enough, mid-teens price |

### Satellite (smaller size, higher IV)
| Ticker | Role | Why it is here |
|---|---|---|
| `MARA` | High-IV premium | Crypto miner, fat premiums, gap risk |
| `CLSK` | High-IV premium | Similar to MARA — do **not** run both at full size together |
| `SNAP` | High-IV / social | Cheap stock, active options |
| `GME` | Spec / residual meme | Use only if chain is liquid that period |
| `AMC` | Spec / residual meme | Tiny share price; spreads can be wide — size tiny or skip |
| `NOK` or `NU` | Optional substitute | Use if AAL/RIVN data is thin |

**Correlation rule:** Treat `MARA` + `CLSK` as one risk bucket. Max 1 miner trade at a time unless the backtest explicitly tests a diversified miner book.

**INTC is excluded** from this small-account spec. It is liquid but no longer a low-price-point name.

---

## 3. Data requirements

### Minimum viable data
- Daily OHLCV for each ticker, split-adjusted
- Risk-free rate proxy (3-month T-bill or SOFR)
- Dividend dates/amounts for `F` (and others if paid)
- VIX daily close

### Options data (preferred → fallback)

**Preferred:** Historical option chains (OHLC or bid/ask/mid, volume, OI, IV) for weeklies and monthlies, 2019–present (or longest available).

**If full chains are unavailable**, the agent **must**:
1. State that clearly at the top of the report.
2. Use a **replicable approximation**:
   - Estimate IV from 20-day realized vol × a premium multiplier, or from VIX scaled by ticker beta/historical IV ratio.
   - Price options with Black-Scholes / binomial (American if dividend).
   - Apply a **fill penalty**: sell at 90% of model mid, buy wings at 110% of model mid.
3. Still enforce defined-risk P&L (width − credit).
4. Run a **stock-only sanity check** (see Strategy A overlay) so results are not 100% model-dependent.

**Do not** pretend model fills equal live fills on AMC/SNAP/GME.

**Backtest window:**  
- Primary: `2021-01-01` through latest available date  
- Walk-forward: train `2021–2023`, test `2024–present`  
- Also report `2022` (rate-shock year) and `2024–2026` separately

---

## 4. Shared execution rules (all strategies)

Apply these unless a strategy overrides them.

### Position sizing
```
risk_dollars = equity * 0.02
max_loss_per_spread = (wing_width - net_credit) * 100
contracts = floor(risk_dollars / max_loss_per_spread)
contracts = min(contracts, floor((equity * 0.25) / capital_required))
if contracts < 1: skip trade
```

For covered calls / CSP:
```
shares = 100 * contracts
capital_required = stock_price * shares   # or strike * shares for cash-secured puts
```

### Management
- Take profit at **50% of max credit** (default)
- Time stop: close at **21 DTE** if still open (monthly) or **2 DTE** (weeklies)
- Hard stop: close if unrealized loss ≥ **1.5× credit received** (or 2× for condors)
- No adding to losers
- After a stop-out on a ticker, sit out **5 trading days**

### Filters (momentum + consistency)
A trade is allowed only if **at least 2 of 3** are true:

1. **Trend:** close > 50-day SMA (bullish structures) or inside 20-day range (neutral structures)
2. **Liquidity:** 20-day average stock volume ≥ 5,000,000 (`AMC`/`GME` exception: ≥ 2,000,000) **and** estimated option bid-ask not grotesque
3. **Vol regime:**
   - Credit spreads / condors: IV Rank ≥ 30 **or** 20-day HV ≥ 25%
   - Covered calls: always allowed if long stock is already a core holding; skip selling calls if IV Rank < 15 and expected credit < $0.15

### Regime tags to record
Tag every trade with: `VIX_bucket` (<15, 15–20, 20–30, >30), `IVR_bucket`, `trend_up/down/sideways`.

---

## 5. Strategy 1 — Core Covered Call / Wheel Lite  
**Name:** `S1_WHEEL_CORE`  
**Tickers:** `F`, `SOFI` (primary). Optional add `AAL` if assigned too rarely.  
**Bias:** Mildly bullish + income  
**Why it fits a small account:** 100 shares of F ≈ $1,400. Defined path. High repeatability.

### Rules
**Phase A — Cash-secured put (if flat)**
- DTE: 21–45 (prefer monthly) or 7–14 weeklies if credit is adequate
- Short put delta: **0.20–0.30** (approx 1.0–1.5 ATM straddle moves OTM if no greeks)
- Fallback strike rule if no greeks: strike = `min(50DMA, close * 0.94)` rounded to listed strike
- Buy a wing 1–2 strikes below (make it a **put credit spread**, not a naked CSP) so max loss is defined
- Width: `$1` on F / SOFI if listed; else `$2`
- Entry credit minimum after costs: **$0.15** ($1-wide) or **8% of width**
- If assigned / if short put finishes ITM: convert to long shares and go to Phase B

**Phase B — Covered call (if long 100-share lots)**
- Sell call delta **0.20–0.30** or strike = `close * 1.04` to `1.07`
- Same DTE logic as puts
- If called away: restart Phase A
- If stock drops 12% from entry: stop selling calls, evaluate exit of shares (backtest both: hold vs. exit)

**Dividend:** On F, do not sell calls with ex-div inside 3 days if call delta > 0.35 (early exercise risk). If data too coarse, flag and skip those weeks.

### What “win” looks like
Small, repeated credits. Assignment is not a failure if shares are sold later at a net credit.

### Extra metric
Track: % of time in stock vs. in short put, yield on assigned cost basis, called-away frequency.

---

## 6. Strategy 2 — Bull Put Credit Spreads  
**Name:** `S2_BULL_PUT`  
**Tickers:** `F`, `SOFI`, `AAL`, `RIVN`  
**Bias:** Mildly bullish / “don’t crash”  
**Why it fits:** Defined risk, no need to own shares, works in a grind or gentle uptrend.

### Rules
- DTE: **21–45** primary; optional weekly sleeve with half size
- Structure: sell put, buy lower put
- Short delta **0.20–0.25** (fallback: short strike ≈ close − 1.0 × 20-day ATR)
- Width: `$1` preferred, `$2` max
- Min credit: **20% of width** after estimated slippage (`$0.20` on a `$1` spread)
- Trend filter **required**: close > 50-day SMA **and** 50-day SMA slope ≥ 0 over 10 days
- Skip if earnings within 7 days (if calendar available). If earnings calendar missing, skip the week of known typical print or document the limitation
- Management: 50% profit, 21 DTE time stop, 1.5× credit loss stop
- Max 2 bull-put positions at once across the book

### Invalidation
If close breaks below the 50-day SMA by > 2% on a closing basis, do not open new trades. Existing trades follow stops only.

---

## 7. Strategy 3 — Defined-Risk Iron Condor (high-IV names, small size)  
**Name:** `S3_IC_SATELLITE`  
**Tickers:** `MARA`, `CLSK`, `SNAP` (primary). `GME` only if average option volume that month is healthy. `AMC` optional / micro-size only.  
**Bias:** Neutral / mean-reversion after elevated vol  
**Why it fits:** These names pay more premium. Small account can still sell `$1–$2` wide condors.

### Rules
- DTE: **10–25**
- Only when **IV Rank ≥ 40** or 20-day HV ≥ 40% **and** price is not making a 20-day high or low on entry day (avoid breakout days)
- Short put and short call ≈ **16-delta** each (fallback: ±1.2 × 20-day ATR from close)
- Wings `$1` or `$2`
- Min total credit: **25% of one-side width** (example: `$0.25` on `$1` wings each side, i.e. $1 wide each)
- Correlation cap: never `MARA` and `CLSK` concurrently
- Management:
  - Take profit at **50% of total credit**
  - If one side is tested (underlying trades through short strike): close the tested side or the whole condor — backtest **both** variants and report which is more consistent
  - Time stop at 5 DTE
  - Loss stop at **2× credit**

### Variant to test (required)
`S3b_PUT_CREDIT_ONLY`: same entry filters, but only sell the put spread (skip call side) when close > 20-day SMA. This often improves consistency on names with upside melt-ups.

---

## 8. Optional 4th sleeve (only if time allows)

**Name:** `S4_MOMO_CC_OVERLAY`  
Long the 2 core names with the strongest 20-day relative strength vs. the universe, hold 100-share lots only if they pass volume + trend filters, sell 1-week 0.25-delta calls. This is a **momentum + income** hybrid. Size at 1% risk equivalent. Compare vs. buy-and-hold of the same names.

---

## 9. Portfolio construction for the agent to run

Run these books separately **and** as a combined book:

| Book | Mix | Max risk |
|---|---|---|
| Conservative | S1 70% + S2 30% | 2% per trade |
| Balanced | S1 40% + S2 40% + S3 20% | 2% per trade; S3 half-size |
| Aggressive-income | S2 50% + S3 50% | Still 2% per trade |

Combined book rules:
- Max 4 open structures
- Max 1 satellite condor
- If drawdown from peak > 12%, halt new satellite trades until equity makes a 5-day higher low

---

## 10. Backtest implementation guidance

### Preferred stack
Python. Use whatever the environment has:
- Prices: `yfinance`, `polygon`, Stooq, or local CSV
- Backtest engine: `vectorbt`, `backtrader`, `zipline-reloaded`, or a simple event loop
- Options pricing fallback: `py_vollib`, `mibian`, or custom BS
- If the agent has a broker/research API with historical options, use it and say so

### Event loop (recommended if no options engine)
Daily:
1. Update equity, mark open positions with model or last mid
2. Check exits (profit, stop, DTE, assignment)
3. Check filters
4. If slots open, scan tickers by priority: F → SOFI → AAL → RIVN → SNAP → MARA/CLSK
5. Place new trades
6. Log every decision, including skips and why

### Assignment modeling
- American options: if ITM at expiration, assume assignment
- For calls on F near ex-div, assume early assignment if remaining time value < dividend
- After assignment, next bar starts as long stock (S1) or realized loss at intrinsic (spreads)

### Avoid look-ahead
- Signals from `close[t]` fill at `open[t+1]` **or** same-day close with a worse fill. Pick one and keep it consistent.
- IV Rank must use only past data.

---

## 11. Metrics the agent must report

### Per strategy and per combined book
- CAGR, total return, max drawdown, MAR ratio (CAGR / |max DD|)
- Win rate (trades and months)
- Profit factor
- Average winner / average loser
- Average days in trade
- % of months profitable
- Return on average allocated capital (not just starting equity)
- Worst 5 trades and why
- Sensitivity: commissions $0 / $0.65 / $1.00
- Sensitivity: take-profit 40% vs 50% vs 70%
- Walk-forward OOS vs IS

### Per ticker
- Trades, expectancy, max DD contribution
- Skip rate due to filters

### Consistency score (required)
```
consistency = 0.40 * pct_profitable_months
            + 0.30 * (1 - max_dd)
            + 0.20 * min(profit_factor / 2, 1)
            + 0.10 * min(win_rate, 1)
```
Rank strategies by this score, not by raw P&L.

### Plots / tables to produce
1. Equity curve vs. buy-and-hold equal-weight universe
2. Underwater (drawdown) chart
3. Monthly returns heatmap
4. Trade log CSV
5. Rolling 63-day win rate

---

## 12. Deliverables the agent must return

1. **`REPORT.md`** — plain-language findings: which strategy was most consistent, which tickers helped vs. hurt, whether the small-account constraint was binding, and what would break in live trading.
2. **`trades.csv`** — one row per trade: date in/out, ticker, strategy, strikes, credit, max loss, P&L, exit reason, regime tags.
3. **`equity.csv`** — daily equity for each book.
4. **Charts** saved as PNG.
5. **Assumptions appendix** — data source, option pricing method, fill model, skipped tickers, bugs.

If results look too good (CAGR > 40% with DD < 8% on option selling), the agent must **distrust them**, widen slippage, and rerun. Premium-selling backtests are usually too optimistic without realistic fills.

---

## 13. Pass / fail bar for “solid”

A strategy is **solid enough to discuss live** only if OOS (2024–present) meets **all**:
- Max drawdown ≤ 18%
- At least 58% of months profitable
- Profit factor ≥ 1.15 after $0.65 commissions
- No single ticker > 50% of total profits (avoid curve-fit to one miner melt-up)
- Average losing trade is understood (not one mystery gap that the model missed)

If none pass, report the closest strategy and the exact rule change that would be needed — do not force a winner.

---

## 14. One-shot agent instruction

Build a reproducible Python backtest that implements `S1_WHEEL_CORE`, `S2_BULL_PUT`, and `S3_IC_SATELLITE` on the ticker universe above with a $10,000 account, 2% risk per trade, defined-risk only, 50% profit-taking, and the shared filters. Use real option chains if available; otherwise document a Black-Scholes fill-penalized approximation. Output REPORT.md, trades.csv, equity.csv, and charts. Optimize nothing against the full sample. Rank by the consistency score. Call out data limitations in the first paragraph of the report.
