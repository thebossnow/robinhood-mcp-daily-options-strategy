"""SPY iron butterfly: a defined-risk, short-premium structure. PAPER ONLY.

Why an IRON butterfly and not a long call/put butterfly
-------------------------------------------------------
Everything in the income framework SELLS premium for a net credit and is
managed on "percent of credit captured": the 50% profit target, the
min(21 DTE, half the entry DTE) time exit, the IV-spike grading, and
`risk/sizing.py`'s capital-at-risk = width - credit. An iron butterfly
(sell the ATM put and ATM call at the same strike, buy a put and a call one
wing-width away) is a credit structure with exactly that risk shape, so it
reuses `CreditPosition`, the paper broker, the journal, the sizing tiers,
the event blackout and the managed backtest unchanged. A long call/put
butterfly is a DEBIT trade: its max loss is the debit, its profit target
and stop would have to be re-expressed in debit terms, and none of the
credit-book plumbing would apply. Same payoff family, wrong book.

Shape: short straddle at the strike nearest spot, wings at
`wing_width_frac * spot` on each side (snapped to listed strikes). The
credit is large relative to width (often 40-60%), but so is the chance of
giving it back: the profit zone is narrow. That is why this structure gets
a loss stop (`stop_loss_frac`) that no other variant uses -- and why it is
OFF by default (`enable_iron_butterfly` in configs/income_framework.json,
or `scan_income.py --butterfly` for a single run).
"""

from __future__ import annotations

import pandas as pd

from .credit import (CreditPosition, CreditVariantConfig, _leg, _pick_wing,
                     _usable_quote, _with_deltas)

IRON_FLY = CreditVariantConfig(
    name="spy_iron_fly",
    structure="iron_butterfly",
    # Informational: the shorts are ATM by construction (~0.50 delta); the
    # off-target guard in credit.py allows up to 1.5x, i.e. 0.75.
    short_put_delta=0.50, short_call_delta=0.50,
    wing_width_frac=0.03,
    min_dte=30, max_dte=45, target_dte=38,
    # Rejects broken quotes, not normal entries: a 30-45 DTE SPY fly with
    # 3% wings normally collects far more than 30% of width.
    min_credit_frac=0.30,
    profit_take_frac=0.50,
    time_exit_dte=21, time_exit_frac=0.5,
    # Close if the loss to close reaches 1x the credit received. A judgment
    # default, not a measured one — flagged for the operator.
    stop_loss_frac=1.0,
    exit_on_breach=False,   # the shorts are ATM; a "breach" stop would fire
                            # on the first tick and is meaningless here
)

BUTTERFLY_VARIANTS: dict[str, CreditVariantConfig] = {IRON_FLY.name: IRON_FLY}


def build_iron_butterfly(chain: pd.DataFrame, spot: float, underlying: str,
                         entry_date: str, expiration: str, dte: int,
                         cfg: CreditVariantConfig,
                         slippage_half_spread_frac: float = 0.5
                         ) -> CreditPosition | None:
    """Short put + short call at the same ATM strike, long wings outside.
    Returns None if any leg is missing/unquoted or the credit gate fails."""
    t_years = max(dte, 1) / 365.0
    puts = chain[chain["type"] == "put"]
    calls = chain[chain["type"] == "call"]
    if puts.empty or calls.empty:
        return None
    puts = _with_deltas(puts, "put", spot, t_years)
    calls = _with_deltas(calls, "call", spot, t_years)
    pq = puts[puts.apply(_usable_quote, axis=1)]
    cq = calls[calls.apply(_usable_quote, axis=1)]
    common = sorted(set(pq["strike"]) & set(cq["strike"]))
    if not common:
        return None
    body = min(common, key=lambda k: abs(k - spot))
    short_put = pq[pq["strike"] == body].iloc[0]
    short_call = cq[cq["strike"] == body].iloc[0]
    if float(short_put["bid"]) < cfg.min_short_bid or \
            float(short_call["bid"]) < cfg.min_short_bid:
        return None
    put_wing = _pick_wing(puts, body, spot, cfg.wing_width_frac, "put")
    call_wing = _pick_wing(calls, body, spot, cfg.wing_width_frac, "call")
    if put_wing is None or call_wing is None:
        return None

    legs = [_leg(short_put, "put", -1), _leg(put_wing, "put", +1),
            _leg(short_call, "call", -1), _leg(call_wing, "call", +1)]
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
    if len(widths) != 2 or min(widths.values()) <= 0:
        return None
    max_width = max(widths.values())
    pos.credit_frac = round(credit_mid / max_width, 4)
    pos.max_loss = round(max_width - credit, 4)
    if pos.credit_frac < cfg.min_credit_frac or pos.max_loss <= 0:
        return None
    return pos
