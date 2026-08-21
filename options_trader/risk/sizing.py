"""Tiered position sizing for the income (premium-selling) framework.

The rest of this repo sizes off a single `max_risk_per_trade_pct` knob in
StrategyConfig (1% by default). The income framework instead specifies a
*schedule* that loosens as the account shrinks:

    equity <= $500      ->  10% of equity
    $501 .. $1,000      ->   7% of equity
    equity >  $1,000    ->   5% of equity

and one binding rule: **size so that a full assignment or the structure's
max loss stays inside that budget**. That second clause is what this module
actually enforces, and it is where the schedule collides with reality.

Two different numbers can be "the risk" of a short-premium position:

  defined-risk spread   (width - credit) * 100        -- capped, small
  cash-secured put      (strike - credit) * 100       -- the assignment bill

For a defined-risk structure the budget is generous: a $1-wide SPY condor
risks well under $100, so a four-figure account can carry one. For a
cash-secured put the budget is the whole collateral, and on an index ETF
that is tens of thousands of dollars. `min_equity_for_one_contract()` makes
the implied minimum explicit instead of letting the caller discover it as a
silent zero-contract result: at a 5% tier, ONE SPY cash-secured put needs
roughly (strike * 100) / 0.05 in equity -- seven figures at current index
levels. That is not a bug in the schedule; it is the schedule correctly
reporting that index CSPs are not a small-account structure.

Sizing here never rounds up and never returns a partial contract. If one
contract does not fit, the answer is zero contracts and a reason string --
the same "NO QUALIFYING TRADE is a valid outcome" posture the scanner takes.

This module decides SIZE only. It is deliberately separate from
RiskManager, which decides PERMISSION (kill switch, daily loss limit, vol
regime, concurrency). A position must clear both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class SizingTier:
    """One band of the schedule. `max_equity` is an inclusive upper bound;
    None means "everything above the previous band"."""
    max_equity: float | None
    risk_pct: float

    def label(self) -> str:
        if self.max_equity is None:
            return f"above previous band: {self.risk_pct:.0%}"
        return f"up to ${self.max_equity:,.0f}: {self.risk_pct:.0%}"


# The framework's schedule, verbatim. Ordered ascending by max_equity.
DEFAULT_TIERS: tuple[SizingTier, ...] = (
    SizingTier(500.0, 0.10),
    SizingTier(1000.0, 0.07),
    SizingTier(None, 0.05),
)


def tier_for(equity: float,
             tiers: tuple[SizingTier, ...] = DEFAULT_TIERS) -> SizingTier:
    """The band `equity` falls into. Bands are inclusive of their upper
    bound, so exactly $500 gets 10% and exactly $1,000 gets 7% -- matching
    the framework's "under 500 / between 501 and 1,000 / 1,000 or more"
    wording, which leaves $500.01..$500.99 ambiguous and treats $1,000
    itself as the top band. We resolve both edges downward (the more
    conservative reading is the smaller budget, but the smaller BAND is
    what the wording names, so $1,000 -> 7% rather than 5%)."""
    if not tiers:
        raise ValueError("tiers must not be empty")
    for t in tiers:
        if t.max_equity is not None and equity <= t.max_equity:
            return t
    last = tiers[-1]
    if last.max_equity is not None:
        raise ValueError("the final tier must have max_equity=None")
    return last


def risk_budget(equity: float,
                tiers: tuple[SizingTier, ...] = DEFAULT_TIERS) -> float:
    """Dollars of max loss / assignment capital allowed in ONE position."""
    return max(0.0, equity) * tier_for(equity, tiers).risk_pct


def min_equity_for_one_contract(
        capital_at_risk_per_contract: float,
        tiers: tuple[SizingTier, ...] = DEFAULT_TIERS) -> float:
    """Smallest equity at which a single contract fits its own tier budget.

    Solved per band rather than algebraically, because a bigger account can
    sit in a *stingier* band: at 10%/7%/5%, $500 of equity permits $50 of
    risk while $1,001 permits $50.05 -- the budget is not monotonic across
    the boundary, so the first band that can hold the position is the
    answer, and the search must consider each band's own ceiling.
    """
    if capital_at_risk_per_contract <= 0:
        raise ValueError("capital_at_risk_per_contract must be positive")
    floor = 0.0
    for t in tiers:
        needed = capital_at_risk_per_contract / t.risk_pct
        ceiling = t.max_equity if t.max_equity is not None else float("inf")
        if max(needed, floor) <= ceiling:
            return max(needed, floor)
        floor = ceiling
    # Unreachable while the last tier has max_equity=None (inf ceiling).
    raise ValueError("no tier can hold this position")


@dataclass
class SizingDecision:
    contracts: int
    capital_at_risk_per_contract: float   # dollars, one contract
    budget: float                         # dollars allowed by the tier
    tier_pct: float
    equity: float
    reasons: list[str] = field(default_factory=list)
    min_equity_for_one_contract: float = 0.0

    @property
    def feasible(self) -> bool:
        return self.contracts >= 1

    @property
    def total_capital_at_risk(self) -> float:
        return self.contracts * self.capital_at_risk_per_contract

    @property
    def risk_pct_of_equity(self) -> float:
        """Actual fraction of equity the SIZED position puts at risk -- the
        number the framework asks to be reported on every trade. Always <=
        tier_pct, usually well under it, because contracts are integers."""
        if self.equity <= 0:
            return 0.0
        return self.total_capital_at_risk / self.equity

    def summary(self) -> str:
        if not self.feasible:
            return "0 contracts — " + "; ".join(self.reasons)
        return (f"{self.contracts} contract(s), "
                f"${self.total_capital_at_risk:,.2f} at risk "
                f"({self.risk_pct_of_equity:.2%} of ${self.equity:,.2f}; "
                f"tier cap {self.tier_pct:.0%})")


def size_position(equity: float, capital_at_risk_per_contract: float,
                  tiers: tuple[SizingTier, ...] = DEFAULT_TIERS,
                  max_contracts: int | None = None,
                  open_capital_at_risk: float = 0.0,
                  portfolio_heat_cap_pct: float | None = None) -> SizingDecision:
    """Contracts that fit the tier budget, floor-rounded, never below zero.

    `open_capital_at_risk` / `portfolio_heat_cap_pct` add the guardrail the
    framework omits: a per-POSITION cap of 5-10% says nothing about the
    book. Three concurrent positions at the 10% tier is 30% of the account
    riding on correlated short premium, which one gap move settles. Pass a
    heat cap to bound the sum; leave it None to size positions in isolation
    (the framework's literal reading).
    """
    reasons: list[str] = []
    if capital_at_risk_per_contract <= 0:
        return SizingDecision(0, capital_at_risk_per_contract, 0.0, 0.0,
                              equity, ["capital at risk must be positive"])

    tier = tier_for(equity, tiers)
    budget = risk_budget(equity, tiers)
    min_equity = min_equity_for_one_contract(capital_at_risk_per_contract, tiers)

    contracts = int(budget // capital_at_risk_per_contract)
    if contracts < 1:
        reasons.append(
            f"one contract commits ${capital_at_risk_per_contract:,.2f}, over the "
            f"${budget:,.2f} allowed at the {tier.risk_pct:.0%} tier "
            f"(needs ${min_equity:,.2f} of equity)"
        )

    if portfolio_heat_cap_pct is not None and contracts >= 1:
        heat_cap = max(0.0, equity) * portfolio_heat_cap_pct
        room = heat_cap - open_capital_at_risk
        allowed_by_heat = int(room // capital_at_risk_per_contract)
        if allowed_by_heat < contracts:
            if allowed_by_heat < 1:
                reasons.append(
                    f"portfolio heat: ${open_capital_at_risk:,.2f} already at "
                    f"risk against a ${heat_cap:,.2f} book cap "
                    f"({portfolio_heat_cap_pct:.0%} of equity) — no room for "
                    f"another ${capital_at_risk_per_contract:,.2f}"
                )
            contracts = max(0, allowed_by_heat)

    if max_contracts is not None and contracts > max_contracts:
        contracts = max_contracts

    return SizingDecision(
        contracts=max(0, contracts),
        capital_at_risk_per_contract=capital_at_risk_per_contract,
        budget=budget,
        tier_pct=tier.risk_pct,
        equity=equity,
        reasons=reasons,
        min_equity_for_one_contract=min_equity,
    )


def spread_capital_at_risk(width: float, credit: float) -> float:
    """Max loss of a defined-risk credit spread / condor, in dollars.
    `width` and `credit` are per share; the widest side governs a condor
    (both sides cannot finish in the money)."""
    return max(0.0, width - credit) * CONTRACT_MULTIPLIER


def csp_capital_at_risk(strike: float, credit: float) -> float:
    """Collateral a cash-secured put ties up, in dollars.

    This is assignment capital, not a probabilistic loss estimate: on
    assignment you buy 100 shares at `strike` having already banked
    `credit`. The absolute worst case (underlying -> 0) is the same number,
    which is why the framework's "full assignment ... within that limit"
    clause and a max-loss reading agree here.
    """
    return max(0.0, strike - credit) * CONTRACT_MULTIPLIER
