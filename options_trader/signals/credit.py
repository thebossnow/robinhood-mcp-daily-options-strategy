"""Credit-structure candidate builders: put credit spreads and iron condors.

These are premium-SELLING structures. Unlike the debit-spread scanner in
candidates.py, there is no model-EV filter here — selection is mechanical
(delta targets, DTE window, credit minimums) and the edge claim rests
entirely on the historical backtest (backtest/managed.py), not on a
probability model scoring the market's own prices.

Three shipped variants (VARIANTS):
    put_spread   — sell ~30-delta put, buy a wing below (bullish lean + VRP)
    condor_sym   — symmetric iron condor, ~20-delta shorts both sides
    condor_asym  — condor with the call side pushed further out (~12 delta);
                   on drifting-up indexes the short call is the chronic
                   loser, so the asymmetric book gives it more room

Strike selection uses per-contract delta when the chain provides it
(DoltHub EOD data does); otherwise Black-Scholes delta is computed from
the leg's implied vol. Wings are placed a fraction of spot away from the
short strike, snapped to the nearest available strike.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from math import erf, log, sqrt

import pandas as pd


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_delta(option_type: str, spot: float, strike: float, iv: float,
             t_years: float, rate: float = 0.0) -> float:
    """Black-Scholes delta; fallback when the chain has no delta column."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    d1 = (log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / (iv * sqrt(t_years))
    if option_type == "call":
        return _norm_cdf(d1)
    if option_type == "put":
        return _norm_cdf(d1) - 1.0
    raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")


def bs_price(option_type: str, spot: float, strike: float, iv: float,
             t_years: float, rate: float = 0.0) -> float:
    """Black-Scholes price (rate defaults to 0, matching the rest of the
    pipeline). Degenerates to intrinsic value at zero time or vol. Used to
    mark held legs on days the EOD dataset doesn't quote their expiration."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 0 or iv <= 0:
        if option_type == "call":
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)
    v_sqrt_t = iv * sqrt(t_years)
    d1 = (log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / v_sqrt_t
    d2 = d1 - v_sqrt_t
    if option_type == "call":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


@dataclass
class CreditVariantConfig:
    """One tradeable variant. Deltas are absolute values."""
    name: str
    short_put_delta: float | None = 0.30    # None = no put side
    short_call_delta: float | None = None   # None = no call side
    # Wing distance as a fraction of spot (snapped to available strikes).
    wing_width_frac: float = 0.02
    # --- Entry gates ---
    # The classic playbook says 30-45 DTE, but the DoltHub EOD dataset
    # quotes expirations at rotating ~{11-16, 25-32, 46} DTE buckets in
    # 2022-2025, which a 30-45 window falls between (1-in-4 weeks hit).
    # 25-50 targeting 45 captures ~96% of weeks with the same intent.
    min_dte: int = 25
    max_dte: int = 50
    target_dte: int = 45
    # Reject entries whose net credit is under this fraction of the widest
    # side's width (risk-reward floor). Recorded on every position either way.
    min_credit_frac: float = 0.20
    min_short_bid: float = 0.05    # short legs must have a real market
    # --- Management (exits, checked on EOD marks in this order) ---
    profit_take_frac: float = 0.50   # close at 50% of entry credit captured
    exit_on_breach: bool = True      # close when spot crosses a short strike
    # Close regardless at min(time_exit_dte, time_exit_frac * entry DTE):
    # 21 DTE for a ~45 DTE entry, proportionally sooner for shorter entries
    # so a 25-DTE entry isn't force-closed days after opening.
    time_exit_dte: int = 21
    time_exit_frac: float = 0.5

    def time_exit_threshold(self, dte_at_entry: int) -> int:
        return min(self.time_exit_dte, int(dte_at_entry * self.time_exit_frac))

    def to_dict(self) -> dict:
        return asdict(self)


# Paper-trading candidates, selected by the 2022-2026 DoltHub sweep
# (16 configs, in-sample 2022-24 / out-of-sample 2025-26, SPY-only):
#   spy_condor15: +$8.84/trade, 68.7% win, PF 1.10 over 211 trades
#   spy_put10:    +$6.01/trade, 81.0% win, PF 1.24 over 211 trades
# Both were positive in-sample AND out-of-sample, but the magnitudes are
# statistically indistinguishable from breakeven (t < 1, best-of-16
# selection bias) — paper trading exists to measure fill quality and
# daily-management uplift, not to confirm an already-proven edge.
# min_credit_frac gates are the 5th percentile of measured entry credit
# fractions, so they reject broken quotes, not normal entries.
# The breach stop is OFF: it was the sweep's dominant loss driver
# (46% of trades stopped on touch, all losers, -$160 average).
VALIDATED: dict[str, CreditVariantConfig] = {
    "spy_condor15": CreditVariantConfig(
        name="spy_condor15", short_put_delta=0.15, short_call_delta=0.15,
        wing_width_frac=0.04, min_credit_frac=0.06, exit_on_breach=False,
    ),
    "spy_put10": CreditVariantConfig(
        name="spy_put10", short_put_delta=0.10, short_call_delta=None,
        wing_width_frac=0.02, min_credit_frac=0.03, exit_on_breach=False,
    ),
    # Weekly short-DTE put spread: 7-14 DTE, 10-delta short, 2% wing.
    # Not backtested over the full sweep (DoltHub lacks weekly expirations
    # in 2022-23); added for fast data accumulation in the paper phase —
    # expect ~1 closed trade/week vs 6-7 weeks for the 45-DTE variants.
    # time_exit_frac=0.5 → exits at ~4 DTE (half of 7-10 DTE entry).
    "spy_weekly_put10": CreditVariantConfig(
        name="spy_weekly_put10", short_put_delta=0.10, short_call_delta=None,
        wing_width_frac=0.02, min_credit_frac=0.02, exit_on_breach=False,
        min_dte=5, max_dte=14, target_dte=7,
        time_exit_dte=4, time_exit_frac=0.5,
    ),
}
# The sweep found every config negative once XLF/XLE were included; the
# validated universe is deliberately index-only.
VALIDATED_UNIVERSE = ["SPY"]

# Original playbook-parameter variants, kept for reproducibility of the
# PR #14 backtest (all three measured negative after costs).
VARIANTS: dict[str, CreditVariantConfig] = {
    # min_credit_frac 0.15: at a 30-delta short with ~2%-of-spot wings the
    # market pays ~0.15-0.25 of width; the folklore "1/3 of width" is not
    # achievable at this delta/width combination (measured on SPY chains).
    "put_spread": CreditVariantConfig(
        name="put_spread", short_put_delta=0.30, short_call_delta=None,
        min_credit_frac=0.15,
    ),
    "condor_sym": CreditVariantConfig(
        name="condor_sym", short_put_delta=0.20, short_call_delta=0.20,
    ),
    "condor_asym": CreditVariantConfig(
        name="condor_asym", short_put_delta=0.20, short_call_delta=0.12,
    ),
    # Same-day / next-day expiration, theta-harvest condor. NOTE: the
    # backtest engine's cadence is weekly (see managed.py) — the next
    # checkpoint after entry is ~5 trading days later, by which point a
    # 0-1 DTE position has always already expired. So profit_take/breach/
    # time_exit never actually fire for this variant: every trade is
    # force-settled at real intrinsic value on the real expiration date
    # (via the daily spot lookup), i.e. this measures "sell 0DTE premium,
    # hold to expiry, no intraday management" rather than a managed book.
    "condor_0dte": CreditVariantConfig(
        name="condor_0dte", short_put_delta=0.15, short_call_delta=0.15,
        wing_width_frac=0.01, min_credit_frac=0.01, min_short_bid=0.02,
        exit_on_breach=False, min_dte=0, max_dte=1, target_dte=0,
    ),
    # Single-leg decomposition of condor15/spy_condor15 (VALIDATED): same
    # delta/wing/DTE/breach params, put and call sides isolated. Answers
    # "which leg drove spy_condor15's SPY/QQQ losses" — the README's condor
    # decomposition only ever measured the two legs bundled together.
    "put_spread_15": CreditVariantConfig(
        name="put_spread_15", short_put_delta=0.15, short_call_delta=None,
        wing_width_frac=0.04, min_credit_frac=0.03, exit_on_breach=False,
    ),
    "call_spread_15": CreditVariantConfig(
        name="call_spread_15", short_put_delta=None, short_call_delta=0.15,
        wing_width_frac=0.04, min_credit_frac=0.03, exit_on_breach=False,
    ),
}


@dataclass
class CreditLeg:
    type: str          # 'call' | 'put'
    strike: float
    side: int          # -1 = short (sold), +1 = long (bought wing)
    entry_bid: float
    entry_ask: float
    entry_delta: float
    entry_iv: float = 0.0   # for model-marking when quotes go missing
    # 'chain' when the feed quoted a delta, 'model' when it was computed
    # from the leg's IV by bs_delta. Neither production provider quotes
    # delta today (CHAIN_COLUMNS has no such column), so 'model' is the
    # normal case live and 'chain' is what DoltHub EOD backtests get.
    # Recorded rather than hidden: a delta this pipeline computed and one
    # the market published are different evidence, and the trade report
    # says which it had.
    delta_source: str = "chain"

    @property
    def entry_mid(self) -> float:
        return (self.entry_bid + self.entry_ask) / 2.0

    @property
    def entry_half_spread(self) -> float:
        return (self.entry_ask - self.entry_bid) / 2.0


@dataclass
class CreditPosition:
    underlying: str
    variant: str
    entry_date: str            # YYYY-MM-DD
    expiration: str            # YYYY-MM-DD
    dte_at_entry: int
    spot_at_entry: float
    legs: list[CreditLeg] = field(default_factory=list)
    credit_mid: float = 0.0    # per share, at mid, before slippage
    credit: float = 0.0        # per share, after entry slippage
    credit_frac: float = 0.0   # credit_mid / widest side's width
    max_loss: float = 0.0      # per share (widest width - credit)

    @property
    def short_put_strike(self) -> float | None:
        return self._short_strike("put")

    @property
    def short_call_strike(self) -> float | None:
        return self._short_strike("call")

    def _short_strike(self, opt_type: str) -> float | None:
        for leg in self.legs:
            if leg.type == opt_type and leg.side == -1:
                return leg.strike
        return None

    def widths(self) -> dict[str, float]:
        """Wing width per side, e.g. {'put': 5.0, 'call': 5.0}."""
        out: dict[str, float] = {}
        for opt_type in ("put", "call"):
            strikes = [l.strike for l in self.legs if l.type == opt_type]
            if len(strikes) == 2:
                out[opt_type] = abs(strikes[0] - strikes[1])
        return out

    def intrinsic_close_cost(self, settlement_price: float) -> float:
        """Per-share cost to close at expiry settlement (intrinsic values)."""
        return intrinsic_close_cost(
            [{"type": l.type, "strike": l.strike, "side": l.side}
             for l in self.legs],
            settlement_price,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["short_put_strike"] = self.short_put_strike
        d["short_call_strike"] = self.short_call_strike
        return d


def intrinsic_close_cost(legs: list[dict], settlement_price: float) -> float:
    """Per-share cost to close a credit position at expiry settlement.
    Works from plain leg dicts (type/strike/side) so the journal can settle
    positions reconstructed from legs_json."""
    cost = 0.0
    for leg in legs:
        if leg["type"] == "call":
            iv = max(0.0, settlement_price - leg["strike"])
        else:
            iv = max(0.0, leg["strike"] - settlement_price)
        cost += -leg["side"] * iv
    return cost


def leg_passes_live_liquidity(row: pd.Series, min_open_interest: int = 100,
                              max_spread_pct: float = 0.10,
                              min_spread_abs: float = 0.05) -> bool:
    """Liquidity gate for LIVE chains (which, unlike DoltHub EOD data, carry
    volume/open_interest). The backtest assumed liquidity; the paper phase
    enforces it: real bid, minimum open interest, and a bid/ask spread no
    wider than max(min_spread_abs, max_spread_pct * mid)."""
    if row["bid"] <= 0 or row["ask"] <= 0 or row["ask"] < row["bid"]:
        return False
    if row.get("open_interest", 0) < min_open_interest:
        return False
    mid = (row["bid"] + row["ask"]) / 2.0
    return (row["ask"] - row["bid"]) <= max(min_spread_abs, max_spread_pct * mid)


# How far past its target delta a short leg may land before the structure
# is refused. Strike selection is "nearest available", which is exact on a
# $1.00 index grid and coarse on a cheap underlying: at F 14.30 on a $0.50
# grid the nearest strike to 0.10 delta was 0.312 (measured 2026-08-21),
# a position carrying roughly three times the intended risk under a name
# that says 10-delta. 1.5x is a tolerance, not a measurement -- no backtest
# sets it -- so it is one named constant rather than a per-variant field.
MAX_SHORT_DELTA_RATIO = 1.5


def short_delta_off_target(pos: "CreditPosition", cfg: "CreditVariantConfig",
                           max_ratio: float = MAX_SHORT_DELTA_RATIO
                           ) -> str | None:
    """Reason string when a short leg is materially riskier than configured,
    else None.

    Guards the RISK-INCREASING side only. A short that lands further OTM
    than asked (F's 0.079 against a 0.10 target) sells less premium than
    modelled, which `min_credit_frac` already judges on its own terms; a
    short that lands NEARER the money is carrying risk the variant never
    authorized, and nothing else in the chain notices.

    A leg whose delta could not be established is not judged here -- the
    trade report's confirmation gate refuses it for that separate reason,
    and reporting "0.0 vs 0.10" would name the wrong fault.
    """
    for leg in pos.legs:
        if leg.side != -1:
            continue
        target = (cfg.short_put_delta if leg.type == "put"
                  else cfg.short_call_delta)
        if not target:
            continue
        actual = abs(leg.entry_delta)
        if actual != actual or actual <= 0:      # NaN or absent
            continue
        if actual > target * max_ratio:
            return (f"short {leg.type} at {leg.strike:g} carries "
                    f"{actual:.3f} delta against a {target:.2f} target "
                    f"({actual / target:.1f}x, limit {max_ratio:.1f}x) — "
                    f"nearest available strike is not the strike this "
                    f"variant asked for")
    return None


def _usable_quote(row: pd.Series) -> bool:
    return row["ask"] > 0 and row["ask"] >= row["bid"] >= 0


def _with_deltas(side: pd.DataFrame, opt_type: str, spot: float,
                 t_years: float) -> pd.DataFrame:
    """Ensure an abs-delta column, from the chain's delta or BS fallback."""
    side = side.copy()
    if "delta" in side.columns and side["delta"].abs().sum() > 0:
        side["abs_delta"] = side["delta"].abs()
    else:
        side["abs_delta"] = side.apply(
            lambda r: abs(bs_delta(opt_type, spot, float(r["strike"]),
                                   float(r["iv"]), t_years)), axis=1)
    return side


def _pick_short(side: pd.DataFrame, target_delta: float,
                min_bid: float) -> pd.Series | None:
    """OTM contract with abs delta closest to target and a real bid."""
    ok = side[(side["abs_delta"] > 0) & (side["abs_delta"] <= 0.5)
              & (side["bid"] >= min_bid)]
    ok = ok[ok.apply(_usable_quote, axis=1)]
    if ok.empty:
        return None
    return ok.loc[(ok["abs_delta"] - target_delta).abs().idxmin()]


def _pick_wing(side: pd.DataFrame, short_strike: float, spot: float,
               wing_width_frac: float, opt_type: str) -> pd.Series | None:
    """Strike nearest to (short ± wing width), strictly further OTM."""
    target_width = wing_width_frac * spot
    if opt_type == "put":
        further = side[side["strike"] < short_strike]
        target = short_strike - target_width
    else:
        further = side[side["strike"] > short_strike]
        target = short_strike + target_width
    further = further[further.apply(_usable_quote, axis=1)]
    if further.empty:
        return None
    return further.loc[(further["strike"] - target).abs().idxmin()]


def _leg(row: pd.Series, opt_type: str, side: int) -> CreditLeg:
    """Build a leg, falling back to the Black-Scholes delta when the feed
    quoted none.

    `row` comes from `_with_deltas`, so `abs_delta` is always present and
    is either the chain's own value or one computed from the leg's IV.
    Without this fallback `entry_delta` is 0.0 on every live entry, which
    silently disables everything downstream that reads it — the report's
    IV/delta confirmation and the whole delta-driven half of the
    IV-spike grading.
    """
    quoted = float(row.get("delta", 0.0) or 0.0)
    if quoted:
        return CreditLeg(
            type=opt_type, strike=float(row["strike"]), side=side,
            entry_bid=float(row["bid"]), entry_ask=float(row["ask"]),
            entry_delta=quoted, entry_iv=float(row.get("iv", 0.0) or 0.0),
            delta_source="chain",
        )
    abs_delta = float(row.get("abs_delta", 0.0) or 0.0)
    signed = abs_delta if opt_type == "call" else -abs_delta
    return CreditLeg(
        type=opt_type, strike=float(row["strike"]), side=side,
        entry_bid=float(row["bid"]), entry_ask=float(row["ask"]),
        entry_delta=signed, entry_iv=float(row.get("iv", 0.0) or 0.0),
        delta_source="model" if abs_delta else "none",
    )


def build_position(chain: pd.DataFrame, spot: float, underlying: str,
                   entry_date: str, expiration: str, dte: int,
                   cfg: CreditVariantConfig,
                   slippage_half_spread_frac: float = 0.5) -> CreditPosition | None:
    """Build one credit position from a single-expiration chain, or None if
    any leg can't be selected or the credit gate fails.

    `chain` columns: type, strike, bid, ask, iv, and optionally delta.
    Entry credit pays slippage: a fraction of each leg's half-spread.
    """
    t_years = max(dte, 1) / 365.0
    legs: list[CreditLeg] = []

    for opt_type, target in (("put", cfg.short_put_delta),
                             ("call", cfg.short_call_delta)):
        if target is None:
            continue
        side = chain[chain["type"] == opt_type]
        if side.empty:
            return None
        side = _with_deltas(side, opt_type, spot, t_years)
        short = _pick_short(side, target, cfg.min_short_bid)
        if short is None:
            return None
        wing = _pick_wing(side, float(short["strike"]), spot,
                          cfg.wing_width_frac, opt_type)
        if wing is None:
            return None
        legs.append(_leg(short, opt_type, side=-1))
        legs.append(_leg(wing, opt_type, side=+1))

    if not legs:
        return None

    credit_mid = sum(-l.side * l.entry_mid for l in legs)
    slip = slippage_half_spread_frac * sum(l.entry_half_spread for l in legs)
    credit = credit_mid - slip
    if credit <= 0:
        return None

    pos = CreditPosition(
        underlying=underlying, variant=cfg.name, entry_date=entry_date,
        expiration=expiration, dte_at_entry=dte, spot_at_entry=spot,
        legs=legs, credit_mid=round(credit_mid, 4), credit=round(credit, 4),
    )
    widths = pos.widths()
    if not widths or min(widths.values()) <= 0:
        return None
    max_width = max(widths.values())
    pos.credit_frac = round(credit_mid / max_width, 4)
    pos.max_loss = round(max_width - credit, 4)
    if pos.credit_frac < cfg.min_credit_frac:
        return None
    if pos.max_loss <= 0:   # degenerate: credit exceeds width (bad quotes)
        return None
    return pos
