"""Cash-secured puts: the framework's other structure.

Everything else in this repo trades DEFINED risk -- verticals and condors,
where the wing caps the loss and `max_loss` is a small, knowable number. A
cash-secured put has no wing. Its loss is capped only by the underlying
going to zero, and the capital it commits is the entire assignment bill:

    collateral = (strike - credit) * 100

That single line is why CSPs get their own module rather than another
`CreditVariantConfig` entry. Reusing the credit-spread path would let a CSP
inherit `max_loss = width - credit`, which for a wingless position is
undefined, and the RiskManager would size it as though the risk were a few
dollars wide. Here the collateral IS the risk number handed to sizing, so a
CSP is refused on a small account instead of being silently mis-sized.

Assignment is a feature of this structure, not a failure of it -- the
framework sells puts at strikes it is willing to own. But "willing to own"
has to be capitalized, and `AssignmentPlan` records what happens next
(take the shares and sell calls against them -- the wheel -- or close
before expiry) at ENTRY, when the decision is cheap, rather than on
expiration Friday when it is not.

Return is reported as return-on-collateral, annualized, because that is the
only figure comparable across strikes and expirations: a $2.00 credit is
excellent on a 30-day $70,000 commitment and unremarkable on a 30-day
$7,000 one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import pandas as pd

from .credit import _pick_short, _usable_quote, _with_deltas

CONTRACT_MULTIPLIER = 100


@dataclass
class CSPConfig:
    """Entry and management parameters for a cash-secured put."""
    name: str = "csp"
    short_put_delta: float = 0.25     # framework: 20-30 delta shorts
    min_dte: int = 30
    max_dte: int = 45
    target_dte: int = 38
    min_short_bid: float = 0.05
    # Reject entries whose annualized return on collateral is below this.
    # A CSP that pays less than cash does is a worse trade than cash, and
    # this is the only gate that catches that -- a delta target alone will
    # happily sell a 25-delta put for three cents in a dead-vol regime.
    min_annualized_return: float = 0.08
    profit_take_frac: float = 0.50    # framework: close at 50% of credit
    # 'wheel' = accept assignment and sell covered calls; 'close' = buy the
    # put back before expiry rather than take stock.
    assignment_plan: str = "wheel"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CSPPosition:
    underlying: str
    entry_date: str
    expiration: str
    dte_at_entry: int
    spot_at_entry: float
    strike: float
    credit_mid: float = 0.0     # per share, before slippage
    credit: float = 0.0         # per share, after entry slippage
    entry_delta: float = 0.0
    entry_iv: float = 0.0
    # 'chain' | 'model' | 'none' — see CreditLeg.delta_source.
    delta_source: str = "chain"
    assignment_plan: str = "wheel"
    variant: str = "csp"
    legs: list[dict] = field(default_factory=list)

    @property
    def collateral(self) -> float:
        """Dollars tied up per contract, net of the credit banked."""
        return max(0.0, self.strike - self.credit) * CONTRACT_MULTIPLIER

    @property
    def max_loss(self) -> float:
        """Per-share worst case (underlying -> 0). Equals collateral/100;
        named separately so callers that speak the credit-spread vocabulary
        get the right magnitude rather than a wing-implied one."""
        return max(0.0, self.strike - self.credit)

    @property
    def breakeven(self) -> float:
        return self.strike - self.credit

    @property
    def discount_to_spot(self) -> float:
        """How far below spot the breakeven sits, as a fraction. This is the
        real 'margin of safety' of the trade -- the underlying can fall this
        much by expiry before the position loses money."""
        if self.spot_at_entry <= 0:
            return 0.0
        return (self.spot_at_entry - self.breakeven) / self.spot_at_entry

    @property
    def return_on_collateral(self) -> float:
        if self.collateral <= 0:
            return 0.0
        return (self.credit * CONTRACT_MULTIPLIER) / self.collateral

    @property
    def annualized_return(self) -> float:
        """Return on collateral scaled to a year. Simple (not compounded)
        scaling: compounding assumes every subsequent cycle finds an equally
        good entry, which is exactly the assumption a premium seller should
        not bake into an entry filter."""
        if self.dte_at_entry <= 0:
            return 0.0
        return self.return_on_collateral * (365.0 / self.dte_at_entry)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(
            collateral=round(self.collateral, 2),
            max_loss=round(self.max_loss, 4),
            breakeven=round(self.breakeven, 4),
            discount_to_spot=round(self.discount_to_spot, 4),
            return_on_collateral=round(self.return_on_collateral, 4),
            annualized_return=round(self.annualized_return, 4),
            structure="cash_secured_put",
        )
        return d


def build_csp(chain: pd.DataFrame, spot: float, underlying: str,
              entry_date: str, expiration: str, dte: int, cfg: CSPConfig,
              slippage_half_spread_frac: float = 0.5) -> CSPPosition | None:
    """Build one cash-secured put, or None if no strike qualifies.

    `chain` columns: type, strike, bid, ask, iv, and optionally delta --
    the same shape `credit.build_position` consumes, so both structures can
    be built from one fetched chain.
    """
    puts = chain[chain["type"] == "put"]
    if puts.empty:
        return None
    t_years = max(dte, 1) / 365.0
    puts = _with_deltas(puts, "put", spot, t_years)
    short = _pick_short(puts, cfg.short_put_delta, cfg.min_short_bid)
    if short is None or not _usable_quote(short):
        return None

    quoted = float(short.get("delta", 0.0) or 0.0)
    abs_delta = float(short.get("abs_delta", 0.0) or 0.0)
    if quoted:
        entry_delta, delta_source = quoted, "chain"
    elif abs_delta:
        # Puts carry negative delta; _with_deltas only ever yields the
        # magnitude, so the sign has to be restored here.
        entry_delta, delta_source = -abs_delta, "model"
    else:
        entry_delta, delta_source = 0.0, "none"

    bid, ask = float(short["bid"]), float(short["ask"])
    credit_mid = (bid + ask) / 2.0
    credit = credit_mid - slippage_half_spread_frac * (ask - bid) / 2.0
    if credit <= 0:
        return None

    strike = float(short["strike"])
    pos = CSPPosition(
        underlying=underlying, entry_date=entry_date, expiration=expiration,
        dte_at_entry=dte, spot_at_entry=spot, strike=strike,
        credit_mid=round(credit_mid, 4), credit=round(credit, 4),
        entry_delta=entry_delta, delta_source=delta_source,
        entry_iv=float(short.get("iv", 0.0) or 0.0),
        assignment_plan=cfg.assignment_plan, variant=cfg.name,
        legs=[{"type": "put", "strike": strike, "side": -1,
               "entry_bid": bid, "entry_ask": ask}],
    )
    if pos.collateral <= 0:
        return None
    if pos.annualized_return < cfg.min_annualized_return:
        return None
    return pos
