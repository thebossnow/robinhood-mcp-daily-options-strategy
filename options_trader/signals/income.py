"""The income framework as two runnable profiles: as-specified, and adjusted.

This module is the seam between a trading framework written in prose and a
pipeline that has already measured several of that framework's parameters
on four and a half years of SPY data. Rather than quietly "fixing" the
prose or blindly implementing it, both readings ship side by side:

  AS_SPECIFIED       The framework verbatim. 20-30 delta shorts, 30-45 DTE
                     monthlies plus expiration-week weeklies, iron condors
                     targeting one third of the spread width, IV rank above
                     30 as a hard preference, cash-secured puts enabled.
                     Runnable, testable, and honest about what it is.

  EVIDENCE_ADJUSTED  The same framework with each parameter this repo has
                     actually measured replaced by the measured answer, and
                     nothing else changed. This is the default.

The differences are not stylistic. Each one is a specific measurement from
the README's 2022-26 SPY sweep (see "Validated variants and the sweep that
produced them") or from this repo's chain measurements:

  short delta 20-30 -> 10-15
      Farther-OTM shorts helped monotonically across the 16-config sweep.
      The 30-delta put spread was among the variants measured NEGATIVE
      after costs (-$47 to -$62 per contract); the surviving configs were
      10-delta and 15-delta.

  condor credit ~1/3 of width -> ~0.06-0.20 of width
      Measured on SPY chains: at 20-30 delta with 2%-of-spot wings the
      market pays roughly 0.15-0.25 of width, and at the validated 15-delta
      / 4%-wing geometry it pays less. One third of width is not a price
      that exists at these deltas; a config that demands it does not select
      a better trade, it selects NO trade, every week, forever. Keeping the
      literal 0.33 floor in AS_SPECIFIED is therefore not a strawman -- it
      is the single most consequential thing to see fail.

  IV rank > 30 as a gate -> recorded, not gating
      A hard IV-rank >= 50 entry filter made sweep results sharply worse:
      high-IV-rank weeks cluster with the trending selloffs that run over
      short strikes. That test was at 50, not 30, so it does not settle the
      question at 30 -- which is exactly why the adjusted profile RECORDS
      IV rank on every entry instead of either trusting or discarding it.

  no explicit stop -> no breach stop, and that is deliberate
      Both profiles run without a short-strike breach stop, because the
      sweep found it fired on 46% of trades, all losers, -$160 average.
      This is one place the framework's silence was already right.

  per-position sizing only -> per-position AND book-level cap
      The framework caps one position at 5-10% of equity and says nothing
      about the book. Three concurrent short-premium index positions at the
      10% tier is 30% of the account on one correlated bet. The adjusted
      profile adds `portfolio_heat_cap_pct`.

  weeklies on Monday of expiration week -> Monday/Tuesday, 3+ DTE floor
      Monday of expiration week is roughly 4 DTE. At 20-30 delta that is a
      gamma-dominated position where a 1% move reprices the short strike
      far faster than theta accrues. The adjusted profile keeps the
      Monday/Tuesday cadence but pushes the weekly window to the 5-14 DTE
      band this repo already paper-trades.

Neither profile is proven profitable. The validated variants' own edge is
statistically indistinguishable from breakeven (README), and the adjusted
profile inherits that caveat rather than escaping it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .credit import CreditVariantConfig
from .csp import CSPConfig
from ..risk.sizing import DEFAULT_TIERS, SizingTier

MONDAY, TUESDAY = 0, 1


@dataclass
class IncomeProfile:
    """One complete configuration of the income framework."""
    name: str
    description: str
    variants: dict[str, CreditVariantConfig]
    csp: CSPConfig | None = None
    # Entry cadence: framework says Monday or Tuesday.
    entry_weekdays: tuple[int, ...] = (MONDAY, TUESDAY)
    # IV-rank preference. None = record only, do not gate.
    min_iv_rank: float | None = None
    iv_rank_use_percentile: bool = True
    # Event handling
    event_blackout_days_before: int = 1
    event_blackout_days_after: int = 1
    typical_event_move_pct: float = 0.01
    # Sizing
    tiers: tuple[SizingTier, ...] = DEFAULT_TIERS
    portfolio_heat_cap_pct: float | None = None
    # Structures a small account may use. CSPs on an index are excluded
    # from the adjusted profile below not by opinion but by arithmetic --
    # see risk/sizing.min_equity_for_one_contract.
    allow_cash_secured_puts: bool = True
    notes: list[str] = field(default_factory=list)

    def entry_day_ok(self, weekday: int) -> tuple[bool, str]:
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
        if weekday in self.entry_weekdays:
            return True, f"{names[weekday]} is an entry day"
        allowed = "/".join(names[d] for d in self.entry_weekdays)
        return False, (f"{names[weekday]} is not an entry day for "
                       f"{self.name} (entries: {allowed})")


# --- The framework, verbatim -------------------------------------------

_AS_SPEC_VARIANTS = {
    # "Sell short strikes at 20 to 30 delta" + "30 to 45 days to expiration"
    "spec_put_spread_25d": CreditVariantConfig(
        name="spec_put_spread_25d", short_put_delta=0.25, short_call_delta=None,
        wing_width_frac=0.02, min_dte=30, max_dte=45, target_dte=38,
        # A put credit spread has no "one third of the width" instruction in
        # the framework -- that clause names iron condors only -- so this
        # keeps the repo's measured-achievable floor.
        min_credit_frac=0.15, exit_on_breach=False, profit_take_frac=0.50,
    ),
    # "For iron condors, collect about one-third of the spread width."
    "spec_condor_20d": CreditVariantConfig(
        name="spec_condor_20d", short_put_delta=0.20, short_call_delta=0.20,
        wing_width_frac=0.02, min_dte=30, max_dte=45, target_dte=38,
        min_credit_frac=0.33, exit_on_breach=False, profit_take_frac=0.50,
    ),
    # "Sell weeklies on Monday or Tuesday of the expiration week."
    "spec_weekly_put_25d": CreditVariantConfig(
        name="spec_weekly_put_25d", short_put_delta=0.25, short_call_delta=None,
        wing_width_frac=0.02, min_dte=1, max_dte=5, target_dte=4,
        min_credit_frac=0.10, exit_on_breach=False, profit_take_frac=0.50,
        time_exit_dte=1, time_exit_frac=0.5,
    ),
}

AS_SPECIFIED = IncomeProfile(
    name="as_specified",
    description="The income framework implemented verbatim, for comparison.",
    variants=_AS_SPEC_VARIANTS,
    csp=CSPConfig(name="spec_csp_25d", short_put_delta=0.25, min_dte=30,
                  max_dte=45, target_dte=38, profit_take_frac=0.50),
    min_iv_rank=0.30,          # "ideally above 30"
    portfolio_heat_cap_pct=None,
    allow_cash_secured_puts=True,
    notes=[
        "spec_condor_20d demands 1/3 of width in credit; SPY chains at 20 "
        "delta with 2% wings pay roughly 0.15-0.25, so this variant is "
        "expected to report NO QUALIFYING TRADE nearly every week.",
        "spec_weekly_put_25d enters at ~4 DTE at 25 delta: gamma-dominated, "
        "and outside any window this repo has backtested.",
        "No book-level risk cap: three 10%-tier positions is 30% of the "
        "account on correlated short premium.",
    ],
)


# --- The framework, with measured parameters substituted ---------------

_ADJUSTED_VARIANTS = {
    # 15-delta condor, 4% wings, no breach stop: sweep-validated, both
    # in-sample and out-of-sample positive (README).
    "income_condor15": CreditVariantConfig(
        name="income_condor15", short_put_delta=0.15, short_call_delta=0.15,
        wing_width_frac=0.04, min_dte=30, max_dte=45, target_dte=40,
        min_credit_frac=0.06, exit_on_breach=False, profit_take_frac=0.50,
    ),
    # 10-delta put spread, 2% wing: the other sweep survivor.
    "income_put10": CreditVariantConfig(
        name="income_put10", short_put_delta=0.10, short_call_delta=None,
        wing_width_frac=0.02, min_dte=30, max_dte=45, target_dte=40,
        min_credit_frac=0.03, exit_on_breach=False, profit_take_frac=0.50,
    ),
    # The weekly, moved off the 4-DTE gamma cliff into the 5-14 DTE band
    # the repo already paper-trades, entered Monday/Tuesday as specified.
    "income_weekly_put10": CreditVariantConfig(
        name="income_weekly_put10", short_put_delta=0.10, short_call_delta=None,
        wing_width_frac=0.02, min_dte=5, max_dte=14, target_dte=7,
        min_credit_frac=0.02, exit_on_breach=False, profit_take_frac=0.50,
        time_exit_dte=4, time_exit_frac=0.5,
    ),
}

EVIDENCE_ADJUSTED = IncomeProfile(
    name="evidence_adjusted",
    description=("The income framework with every parameter this repo has "
                 "measured replaced by the measured answer."),
    variants=_ADJUSTED_VARIANTS,
    # CSPs stay DEFINED as a structure but are switched off by default: on
    # an index at current levels one contract's collateral exceeds any
    # account the tier schedule was written for. Enable explicitly, with a
    # cheaper underlying, once sizing.min_equity_for_one_contract clears.
    csp=CSPConfig(name="income_csp_15d", short_put_delta=0.15, min_dte=30,
                  max_dte=45, target_dte=40, profit_take_frac=0.50),
    allow_cash_secured_puts=False,
    min_iv_rank=None,          # recorded on every entry, never gating
    portfolio_heat_cap_pct=0.20,
    notes=[
        "IV rank is recorded on every entry but gates nothing: the nearest "
        "test this repo ran (a hard IVR>=50 filter) made results sharply "
        "worse, and no test at 30 exists yet.",
        "Cash-secured puts disabled by default — see the sizing arithmetic "
        "in risk/sizing.py; re-enable per underlying once one contract fits.",
        "Book capped at 20% of equity across all open short premium.",
    ],
)

PROFILES: dict[str, IncomeProfile] = {
    AS_SPECIFIED.name: AS_SPECIFIED,
    EVIDENCE_ADJUSTED.name: EVIDENCE_ADJUSTED,
}
DEFAULT_PROFILE = EVIDENCE_ADJUSTED.name


def get_profile(name: str) -> IncomeProfile:
    if name not in PROFILES:
        raise KeyError(f"unknown profile {name!r}; have {sorted(PROFILES)}")
    return PROFILES[name]


def with_underlying_scale(profile: IncomeProfile,
                          wing_width_frac: float) -> IncomeProfile:
    """Same profile with every variant's wing width rescaled.

    Wings are expressed as a fraction of spot so the geometry ports across
    underlyings, but strike GRIDS do not scale: SPY quotes $1 strikes, so a
    4% wing at spot 762 is 30 strikes wide, while a $30 underlying's 4% wing
    rounds to the single nearest strike. Rescaling is how a cheaper
    underlying (the one that makes cash-secured puts affordable) gets wings
    that land on real strikes instead of collapsing onto the short.
    """
    return replace(profile, variants={
        k: replace(v, wing_width_frac=wing_width_frac)
        for k, v in profile.variants.items()
    })
