# Small-Account Options Income Backtest -- REPORT

**Data limitations (read this first):** F/AAL/GME price options off DoltHub's free, community-maintained EOD chain snapshots (no volume/OI; liquidity is NOT verified against real market depth). SOFI/RIVN/SNAP/AMC/MARA/CLSK price off Databento's real OPRA feed (real NBBO), routed in because DoltHub has ~zero usable chain history for those six. Both sources can be missing a quote on a given (ticker, day, strike); when that happens the position is priced via Black-Scholes at the entry implied vol (a model mark, not a live fill) with the entry bid/ask half-spread as the slippage proxy -- flagged per-leg internally as `source in {marks, model, mixed}` but not surfaced in the trade log here. Assignment is modeled (American, ITM-at-expiry), early exercise is NOT modeled except for the F/AAL dividend guard (skip selling a call within 3 days of ex-div if delta > 0.35). **Commission is applied POST-HOC to realized trades in the sensitivity table below, not inside entry gates or position sizing** -- a config that only barely clears its credit-fraction minimum before commission might not clear it after; this run does not model that interaction, so real live P&L would be somewhat worse than the $0.65/$1.00 columns below suggest, not just by the commission delta they show.

## Bottom line

Combined book (S1_WHEEL_CORE + S2_BULL_PUT + S3_IC_SATELLITE, one shared $10,000 account, 2021-01-01 to 2026-08-29, 501 trades): profit factor **0.522**, max drawdown **-59.0%**, final equity **$4,245.03** (started at $10,000). **This fails the spec's section 13 pass bar on every criterion** (needs PF >= 1.15, max DD <= 18%, >= 58% profitable months) -- none of the three strategies, alone or combined, is solid enough to discuss live. See [[robinhood-mcp-small-account-income-backtest]] memory for the prior session's root-cause finding: even under a perfect, zero-overshoot hard-stop, the best achievable combined PF is 0.725 -- this is an edge problem in the rule set itself (fixed delta targets, fixed DTE windows), not a gap-risk or stop-execution problem.

## Consistency score ranking (spec section 11 formula)

`0.40*pct_profitable_months + 0.30*(1-max_dd) + 0.20*min(PF/2,1) + 0.10*min(win_rate,1)`. Per-sleeve rows use that sleeve's OWN isolated equity curve (run alone, not competing for the combined book's 4-structure cap) -- see the capacity note below.

| Rank | Book | Score | % profitable months | Max DD | Profit factor | Win rate |
|---|---|---|---|---|---|---|
| 1 | S3_IC_SATELLITE (isolated) | 0.4713 | 27.9% | 12.2% | 0.397 | 56.5% |
| 2 | S2_BULL_PUT (isolated) | 0.4111 | 29.4% | 36.4% | 0.483 | 54.4% |
| 3 | S1_WHEEL_CORE (isolated) | 0.3638 | 29.4% | 54.4% | 0.492 | 60.0% |
| 4 | COMBINED (S1+S2+S3, shared book) | 0.3518 | 29.4% | 59.0% | 0.522 | 59.1% |

All four books score below 0.5 on a scale that tops out at 1.0 for a perfect record -- there is no close call here; nothing ranks as consistent, only as *less inconsistent*.

## Combined-book capacity constraint (isolated vs. shared-book trade counts)

The combined book caps at 4 open structures total and 1 satellite (S3) position at a time, plus a drawdown halt on new S3 entries past -12% from peak. Running each sleeve ALONE (no competition for those slots) trades far more:

| Sleeve | Trades (in combined book) | Trades (isolated) | Isolated PF | Isolated max DD |
|---|---|---|---|---|
| S1_WHEEL_CORE | 368 | 390 | 0.492 | -54.4% |
| S2_BULL_PUT | 123 | 259 | 0.483 | -36.4% |
| S3_IC_SATELLITE | 10 | 62 | 0.397 | -12.2% |

The small-account constraint IS binding, especially for S2 and S3 (2x and 6x more trades when unconstrained) -- but isolating them doesn't fix the edge problem: S2 alone is still PF 0.483, S3 alone is still PF 0.397. More capacity would have let a bad strategy lose money faster, not turned it profitable.

## Per-ticker breakdown

| Ticker | Trades | Win rate | Total P&L | Expectancy/trade |
|---|---|---|---|---|
| RIVN | 82 | 45.1% | $-1,609.51 | $-19.63 |
| AAL | 138 | 61.6% | $-1,344.50 | $-9.74 |
| F | 135 | 63.0% | $-1,265.62 | $-9.37 |
| SOFI | 136 | 62.5% | $-1,118.09 | $-8.22 |
| SNAP | 5 | 20.0% | $-393.25 | $-78.65 |
| MARA | 5 | 60.0% | $-24.00 | $-4.80 |

No single ticker explains the loss: RIVN is the worst at $-1,609.51 (28% of total losses), but losses are spread across nearly every ticker in the universe -- this rules out a one-ticker curve-fit or a single data-quality outlier as the explanation.

## Commission sensitivity (post-hoc on realized trades)

| Commission/contract/side | Total commission paid | P&L after commission | Profit factor | Expectancy/trade |
|---|---|---|---|---|
| $0.00 | $0.00 | $-5,754.97 | 0.522 | $-11.49 |
| $0.65 | $3,690.70 | $-9,445.67 | 0.3 | $-18.85 |
| $1.00 | $5,678.00 | $-11,432.97 | 0.202 | $-22.82 |

The spec's pass bar (section 13) requires PF >= 1.15 **after $0.65 commissions**; the $0.00 column above is already the headline PF 0.522 number quoted elsewhere in this repo's memory/notes, so every prior mention of that number was, if anything, slightly optimistic.

## Take-profit sensitivity (40% / 50% / 70% of entry credit)

| Profit-take | Trades | Profit factor | Max DD | Total P&L | Final equity |
|---|---|---|---|---|---|
| 0.40 | 536 | 0.477 | -62.3% | $-6,062.74 | $3,937.26 |
| 0.50 (spec default) | 501 | 0.522 | -59.0% | $-5,754.97 | $4,245.03 |
| 0.70 | 447 | 0.573 | -56.6% | $-5,437.38 | $4,562.62 |

Higher profit-take thresholds (holding for more of the credit before closing) modestly IMPROVE every metric here (PF 0.477 -> 0.522 -> 0.573 going 40% -> 50% -> 70%) -- the opposite of the usual short-premium intuition that taking profit early is safer. Consistent with the earlier gap-risk finding: the hard stop (not the profit target) is where this strategy loses money, so anything that reaches the hard stop less often (fewer, longer-held trades) helps a little. Still nowhere near the 1.15 bar at any setting.

## Worst 5 trades

| Underlying | Sleeve | Entry | Exit | Exit reason | P&L |
|---|---|---|---|---|---|
| F | S1_WHEEL_CORE | 2026-05-04 | 2026-05-13 | hard_stop | $-413.63 |
| F | S1_WHEEL_CORE | 2022-08-29 | 2022-09-20 | hard_stop | $-157.68 |
| SNAP | S3_IC_SATELLITE | 2021-10-11 | 2021-10-22 | hard_stop | $-154.00 |
| SOFI | S2_BULL_PUT | 2021-11-15 | 2021-11-22 | hard_stop | $-142.50 |
| F | S1_WHEEL_CORE | 2026-05-26 | 2026-05-28 | hard_stop | $-138.52 |

All five are `hard_stop` exits -- consistent with the prior session's finding that hard-stop exits average -$62.37 vs. profit-target's +$21.55 (a ~2.9x asymmetry), and that even clamping every hard-stop to its theoretical (no-overshoot) bound only lifts combined PF to 0.725, still short of the 1.15 bar. These are understood losses (the mechanism is known and quantified), not mystery gaps the model missed.

## Walk-forward (train 2021-2023 / test 2024-present)

| Period | Trades | Win rate | Profit factor | Total P&L |
|---|---|---|---|---|
| Train (2021-2023) | 263 | 58.9% | 0.513 | $-3,352.28 |
| Test (2024-present) | 238 | 59.2% | 0.533 | $-2,402.69 |

Train and test are consistently bad (PF 0.513 in-sample, 0.533 out-of-sample) -- this is not an overfitting-then-decaying pattern, it never worked in either half.

## Regime breakdown (VIX bucket / trend)

See `runs/income_summary_20210101_20260829.json`'s `by_vix_bucket`/`by_trend_bucket` for the full tables (reproduced in the prior session's walk-forward output above this report) -- IV Rank is NOT computed (this repo has no per-ticker IV-rank infrastructure), so the spec's third regime tag is not available; VIX bucket and 50-day-SMA trend bucket are.

## Charts

All saved under `reports/income_backtest/charts/`:

- `equity_vs_buyhold.png` -- combined book vs. buy-and-hold equal-weight universe
- `underwater.png` -- combined book drawdown
- `monthly_returns_heatmap.png` -- combined book, year x month
- `monthly_returns_S{1,2,3}_*.png` -- each sleeve, isolated
- `rolling_63d_win_rate.png` -- combined book

## Two real bugs found and fixed while building this report (not pre-existing correctness issues from the prior session's memory -- new)

1. `options_trader/data/dolthub.py`'s `build_spot_lookup` stored a NaN Close straight from yfinance without filtering it. A NaN spot satisfies neither `spot <= 0` nor `not spot`, so it sailed through every downstream missing-data guard as if it were a real price, then poisoned every Black-Scholes computation it touched. Fixed by dropping NaN-close rows at the source (same effect as a genuinely missing day). Regression test: `tests/test_dolthub.py::TestBuildSpotLookup`.
2. `options_trader/backtest/managed.py`'s `_close_quotes` checked `if ask <= 0 or ask < bid: bid = ask = None` to reject a bad quote -- but a NaN bid/ask (seen live on a thin Databento RIVN quote) fails BOTH comparisons (NaN comparisons are always False), so it slipped through as a valid mid-price and produced a NaN trade P&L, which then permanently NaN-poisoned every later equity value in the run. Fixed by asserting the GOOD condition instead (`ask > 0 and bid >= 0 and ask >= bid`), which correctly fails closed on NaN. Regression test: `tests/test_managed_backtest.py::TestCloseQuotesNanGuard`. Both bugs were latent in the codebase before this session (not introduced by the profit-take sensitivity sweep) -- the sweep just happened to route an entry/exit sequence through the exact (ticker, day, strike) combination that exposed each one. The base 501-trade run's headline numbers shifted slightly after the fix (total P&L $-5,747.15 -> $-5,754.97, one trade's exit reason changed) but the conclusion is unchanged.

## Known gaps, not fixed (scoping calls made in this pass)

- **Section 9's three named books (Conservative/Balanced/Aggressive-income) were not built as separate runs.** Only one combined book (S1+S2+S3 sharing one 4-structure cap) and each sleeve in full isolation exist. A `Sleeve` weighting/allocation layer to reproduce the exact 70/30, 40/40/20, 50/50 mixes doesn't exist yet.
- **Commission is not wired into entry gates or position sizing**, only applied post-hoc to the realized-trade P&L in the sensitivity table above -- see the data-limitations paragraph at the top.
- **IV Rank regime tag** is not implemented (no per-ticker IV-rank infrastructure in this repo).
- **Skip-reason logging** (spec section 10: "log every decision, including skips and why") is not implemented -- the engine silently returns `None` from `_try_enter` on a failed gate with no counter, so a skip-rate-by-filter table isn't producible from current output without adding instrumentation.
- **$5,000 / $25,000 starting-equity sensitivity** (spec section 1) not run -- only the $10,000 default.
