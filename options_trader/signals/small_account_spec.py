"""Ticker universe and per-strategy configs for the small-account options
income backtest spec (options_income_backtest_spec.md).

Originally narrowed to the 3 tickers this repo's free data source (DoltHub)
actually covers 2021-present: F, AAL, GME. SOFI, RIVN, SNAP, and AMC had
zero DoltHub option-chain rows across every date sampled 2019-2026; MARA
and CLSK only had a few months each -- far short of the multi-year window
the spec wants. See the plan discussion in this repo's PR description for
that coverage probe.

The other 6 spec tickers are back in as of the Databento (OPRA.PILLAR)
source (options_trader/data/databento.py), which covers all of them back
to 2013 -- routed in via MultiSourceHistory (options_trader/data/
multi_source.py) alongside DoltHub for F/AAL/GME, since Databento bills by
data pulled and DoltHub is free where it already works. Restored to their
original spec roles: SOFI/RIVN join the core wheel+bull-put sleeves
(S1/S2), SNAP/AMC/MARA/CLSK join the satellite condor sleeve (S3)
alongside GME.

S2 and S3 need no new state-machine code -- signals/credit.py's
build_position already handles both a single-sided put spread and a
symmetric/asymmetric iron condor generically, so these are config only.
S1 is the wheel (signals/wheel.py), the one strategy this repo couldn't
already build from existing primitives.

Wing widths use credit.py's wing_width_frac (a fraction of spot, snapped
to the nearest listed strike) rather than the spec's literal "$1 or $2
wide" -- GME's price has ranged roughly $10-$350 since 2021, so a fixed
dollar width would be either far too tight or far too loose depending on
the period. This is an approximation, not a hand-tuned fit: it is the same
mechanism credit.py already uses for SPY/QQQ/IWM, chosen before running
any backtest on these tickers.
"""

from __future__ import annotations

from .credit import CreditVariantConfig
from .wheel import WheelConfig

# --- Ticker universe (see module docstring) ---
S1_TICKERS = ["F", "AAL", "SOFI", "RIVN"]
S2_TICKERS = ["F", "AAL", "SOFI", "RIVN"]
S3_TICKERS = ["GME", "SNAP", "AMC", "MARA", "CLSK"]
# Databento-backed tickers (routed to DatabentoHistory; F/AAL/GME stay on
# the free DoltHub source). See multi_source.py.
DATABENTO_TICKERS = ["SOFI", "RIVN", "SNAP", "AMC", "MARA", "CLSK"]

# --- S1_WHEEL_CORE ---
S1_WHEEL_CORE = WheelConfig(
    name="s1_wheel_core",
    put_delta_target=0.25, put_wing_width_frac=0.02,
    put_min_dte=7, put_max_dte=45, put_min_credit_frac=0.08,
    call_delta_target=0.25,
    drop_from_basis_halt_pct=0.12,
)

# --- S2_BULL_PUT ---
# spec: short delta 0.20-0.25, DTE 21-45 primary (+ optional weekly half
# size), min credit 20% of width, no naked short (wing always bought).
S2_BULL_PUT = CreditVariantConfig(
    name="s2_bull_put", short_put_delta=0.225, short_call_delta=None,
    wing_width_frac=0.02, min_dte=21, max_dte=45, min_credit_frac=0.20,
    profit_take_frac=0.50, exit_on_breach=False,
    time_exit_dte=21, time_exit_frac=0.5, hard_stop_credit_mult=1.5,
)
S2_BULL_PUT_WEEKLY = CreditVariantConfig(
    name="s2_bull_put_weekly", short_put_delta=0.225, short_call_delta=None,
    wing_width_frac=0.02, min_dte=5, max_dte=14, min_credit_frac=0.20,
    profit_take_frac=0.50, exit_on_breach=False,
    time_exit_dte=2, time_exit_frac=0.3, hard_stop_credit_mult=1.5,
)

# --- S3_IC_SATELLITE ---
# spec: ~16-delta short put and call, DTE 10-25, min total credit 25% of
# one-side width -- credit_frac here is credit_mid / widest side's width,
# which is exactly that ratio when both sides are the same width.
#
# min_credit_frac is 0.15/0.10 here, not the spec's literal 0.25 -- a
# direct measurement on real GME chains (Jan-Jun 2022 sample, 59 weekly
# entry days) found a 16-delta put spread actually pays 0.11-0.19 of
# width, never 0.25; this repo's own prior SPY measurement (see
# signals/income.py's income_condor15 vs spec_condor_20d) found the same
# thing at a comparable delta ("one third of width is not a price that
# exists at these deltas... a config that demands it selects NO trade,
# every week, forever"). 0.15/0.10 sit below the low end of what was
# measured, so the gate still rejects genuinely weak trades rather than
# admitting everything -- this is a floor correction, not a fit to this
# backtest's results.
#
# Separately, and NOT fixed by the above: S3_IC_SATELLITE's full condor
# frequently fails to build at all (0 trades in a 5.7-year run) because
# the short call's wing has nowhere to go -- DoltHub's community-maintained
# GME chains often don't quote strikes far enough above a ~16-delta call
# during high-IV stretches. That's a data-coverage limit, not a
# threshold problem; S3B_PUT_CREDIT_ONLY (no call side, no wing to place
# out there) doesn't have it.
S3_IC_SATELLITE = CreditVariantConfig(
    name="s3_ic_satellite", short_put_delta=0.16, short_call_delta=0.16,
    wing_width_frac=0.02, min_dte=10, max_dte=25, min_credit_frac=0.15,
    profit_take_frac=0.50, exit_on_breach=False,
    time_exit_dte=5, time_exit_frac=0.2,
    hard_stop_credit_mult=2.0,   # spec: 2x for condors (vs 1.5x elsewhere)
)
# spec's required variant: put side only, for names prone to upside melt-ups.
S3B_PUT_CREDIT_ONLY = CreditVariantConfig(
    name="s3b_put_credit_only", short_put_delta=0.16, short_call_delta=None,
    wing_width_frac=0.02, min_dte=10, max_dte=25, min_credit_frac=0.10,
    profit_take_frac=0.50, exit_on_breach=False,
    time_exit_dte=5, time_exit_frac=0.2, hard_stop_credit_mult=1.5,
)
