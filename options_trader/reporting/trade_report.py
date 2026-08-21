"""The trade report, in the exact shape the income framework asks for.

    "Always confirm the current implied volatility and delta before selling.
     Report every trade with entry price, strike, expiration, credit
     received, and risk percentage."

Those two sentences are a reporting CONTRACT, so they get a dataclass with
required fields rather than a print statement in each script. A report that
cannot be built is a trade that should not be placed: if the chain gave no
delta and no IV, the confirmation the framework requires did not happen,
and `ConfirmationLine.confirmed` is False.

Three additions the framework does not ask for but that a reader of the
report needs to judge it:

  BREAKEVEN         the underlying level at which the trade stops making
                    money. "20-30 delta" is a probability statement;
                    breakeven is the price statement, and only one of them
                    tells you what you are actually short.
  CAPITAL COMMITTED  the dollars tied up, next to the risk percentage.
                    A percentage alone hides whether the position is one
                    contract or six.
  IV RANK           the entry's IV context, recorded on every trade even
                    when it is not gating anything -- that is what makes it
                    possible to test the framework's "IV rank above 30"
                    preference later against this book's own results,
                    rather than arguing about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConfirmationLine:
    """The pre-sale IV/delta confirmation, and whether it actually happened.

    Only a delta the CHAIN quoted counts. `credit.build_position` can select
    strikes from a Black-Scholes delta computed off the leg's IV when the
    feed omits the column, and those positions are perfectly tradeable --
    but a model-inferred delta is this pipeline agreeing with itself, not
    the market confirming anything. The framework asks for a confirmation
    step; a report that showed a self-computed number in that slot would
    turn the step into a formality.
    """
    short_strike: float | None
    short_delta: float | None
    short_iv: float | None
    iv_rank_note: str = ""

    @property
    def confirmed(self) -> bool:
        return self.short_delta is not None and self.short_iv is not None

    def render(self) -> str:
        if not self.confirmed:
            missing = [n for n, v in (("delta", self.short_delta),
                                      ("IV", self.short_iv)) if v is None]
            return (f"NOT CONFIRMED — chain supplied no {' and no '.join(missing)} "
                    f"for the short strike; do not sell on an unconfirmed quote")
        line = (f"confirmed: short {self.short_strike:g} at "
                f"{abs(self.short_delta):.2f} delta, IV {self.short_iv:.1%}")
        return f"{line} | {self.iv_rank_note}" if self.iv_rank_note else line


@dataclass
class TradeReport:
    underlying: str
    structure: str            # 'put_credit_spread' | 'iron_condor' | 'cash_secured_put'
    variant: str
    entry_date: str
    expiration: str
    dte: int
    spot_at_entry: float
    legs: list[str]           # rendered, e.g. '-725P', '+695P'
    credit: float             # per share, after slippage — the credit RECEIVED
    credit_mid: float         # per share, at mid — the credit QUOTED
    contracts: int
    capital_at_risk: float    # dollars, total across contracts
    equity: float
    risk_pct: float           # capital_at_risk / equity
    tier_pct: float           # the sizing tier's cap, for context
    breakeven: float | None
    confirmation: ConfirmationLine
    event_note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def credit_received(self) -> float:
        """Total dollars collected across all contracts."""
        return self.credit * 100.0 * self.contracts

    def render(self) -> str:
        head = (f"{self.underlying} {self.structure} [{self.variant}] "
                f"x{self.contracts}")
        lines = [
            head,
            "  " + "-" * (len(head) - 2),
            f"  entry date       {self.entry_date}  (spot {self.spot_at_entry:.2f})",
            f"  expiration       {self.expiration}  ({self.dte} DTE)",
            f"  strikes          {' / '.join(self.legs)}",
            f"  entry price      {self.credit_mid:.2f} quoted mid, "
            f"{self.credit:.2f} after slippage (per share)",
            f"  credit received  ${self.credit_received:,.2f} total",
            f"  breakeven        " + (f"{self.breakeven:.2f}"
                                      if self.breakeven is not None else "n/a"),
            f"  capital at risk  ${self.capital_at_risk:,.2f}",
            f"  risk percentage  {self.risk_pct:.2%} of ${self.equity:,.2f} "
            f"equity (tier cap {self.tier_pct:.0%})",
            f"  IV/delta check   {self.confirmation.render()}",
        ]
        if self.event_note:
            lines.append(f"  events           {self.event_note}")
        for n in self.notes:
            lines.append(f"  note             {n}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying, "structure": self.structure,
            "variant": self.variant, "entry_date": self.entry_date,
            "expiration": self.expiration, "dte": self.dte,
            "spot_at_entry": self.spot_at_entry, "legs": self.legs,
            "credit_per_share": self.credit,
            "credit_mid_per_share": self.credit_mid,
            "credit_received_total": round(self.credit_received, 2),
            "contracts": self.contracts,
            "capital_at_risk": round(self.capital_at_risk, 2),
            "equity": self.equity, "risk_pct": round(self.risk_pct, 6),
            "tier_pct": self.tier_pct, "breakeven": self.breakeven,
            "iv_delta_confirmed": self.confirmation.confirmed,
            "short_delta": self.confirmation.short_delta,
            "short_iv": self.confirmation.short_iv,
            "event_note": self.event_note, "notes": self.notes,
        }


def _render_legs(legs) -> list[str]:
    return [f"{'-' if l['side'] < 0 else '+'}{l['strike']:g}"
            f"{l['type'][0].upper()}" for l in legs]


def _short_leg(legs, opt_type: str = "put") -> dict | None:
    for l in legs:
        if l["side"] == -1 and l["type"] == opt_type:
            return l
    return None


def report_credit_position(pos, sizing, equity: float, structure: str,
                           iv_rank_note: str = "", event_note: str = "",
                           notes: list[str] | None = None) -> TradeReport:
    """Build a report for a CreditPosition (put spread or iron condor).

    Breakeven is reported for the PUT side, which is the tested side of an
    index condor in every regime that matters -- an index that rallies
    through the call side is a loss too, but it is the selloff that
    produces the fast, gapping version.
    """
    legs = [{"type": l.type, "strike": l.strike, "side": l.side}
            for l in pos.legs]
    short_put = _short_leg(legs, "put")
    short = next((l for l in pos.legs if l.side == -1
                  and (short_put is None or l.strike == short_put["strike"])),
                 None)
    breakeven = (short_put["strike"] - pos.credit) if short_put else None
    return TradeReport(
        underlying=pos.underlying, structure=structure, variant=pos.variant,
        entry_date=pos.entry_date, expiration=pos.expiration,
        dte=pos.dte_at_entry, spot_at_entry=pos.spot_at_entry,
        legs=_render_legs(legs), credit=pos.credit, credit_mid=pos.credit_mid,
        contracts=sizing.contracts,
        capital_at_risk=sizing.total_capital_at_risk, equity=equity,
        risk_pct=sizing.risk_pct_of_equity, tier_pct=sizing.tier_pct,
        breakeven=breakeven,
        confirmation=ConfirmationLine(
            short_strike=short.strike if short else None,
            short_delta=(short.entry_delta if short and short.entry_delta else None),
            short_iv=(short.entry_iv if short and short.entry_iv else None),
            iv_rank_note=iv_rank_note),
        event_note=event_note, notes=list(notes or []),
    )


def report_csp_position(pos, sizing, equity: float, iv_rank_note: str = "",
                        event_note: str = "",
                        notes: list[str] | None = None) -> TradeReport:
    extra = list(notes or [])
    extra.append(
        f"cash-secured: ${pos.collateral:,.0f} collateral per contract, "
        f"{pos.annualized_return:.1%} annualized on collateral, breakeven "
        f"{pos.discount_to_spot:.1%} below spot, on assignment "
        f"-> {pos.assignment_plan}")
    return TradeReport(
        underlying=pos.underlying, structure="cash_secured_put",
        variant=pos.variant, entry_date=pos.entry_date,
        expiration=pos.expiration, dte=pos.dte_at_entry,
        spot_at_entry=pos.spot_at_entry,
        legs=[f"-{pos.strike:g}P"], credit=pos.credit,
        credit_mid=pos.credit_mid, contracts=sizing.contracts,
        capital_at_risk=sizing.total_capital_at_risk, equity=equity,
        risk_pct=sizing.risk_pct_of_equity, tier_pct=sizing.tier_pct,
        breakeven=pos.breakeven,
        confirmation=ConfirmationLine(
            short_strike=pos.strike,
            short_delta=pos.entry_delta or None,
            short_iv=pos.entry_iv or None,
            iv_rank_note=iv_rank_note),
        event_note=event_note, notes=extra,
    )
