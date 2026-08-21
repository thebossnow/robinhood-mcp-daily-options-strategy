"""Implied-volatility rank and percentile.

The income framework asks for "elevated implied volatility rank, ideally
above 30" as an entry preference. Two different statistics get called
"IV rank" in retail material, and they disagree sharply in a year with one
big spike:

  IV RANK        (current - min) / (max - min) over the lookback. Anchored
                 to two extreme observations, so a single March-2020-style
                 print suppresses the rank for the following year.
  IV PERCENTILE  fraction of lookback days whose IV was below current.
                 Distribution-aware and far more stable; this is the one
                 worth acting on.

Both are computed here and both are recorded on every entry, because this
repo's own evidence says the filter should be measured before it is
trusted: the 2022-26 put-credit sweep (README, "Validated variants") found
that a hard IV-rank >= 50 entry filter made results *sharply worse* -- high
IV-rank weeks cluster with the trending selloffs that run over short
strikes. That is one dataset, one delta family and one filter threshold, so
it does not prove IV rank is useless at 30. It does mean a hard gate here
would be adopting an untested rule that the nearest available test failed.

So the default posture is: RECORD always, PREFER softly, GATE only if the
operator explicitly sets a threshold. `IVRankReading.meets()` reports
whether the preference is satisfied; nothing in the pipeline refuses a
trade on it unless `min_iv_rank` is configured.

The IV series itself should be ATM implied vol for a roughly constant
maturity -- `atm_iv()` extracts one day's observation from a chain. VIX is
an acceptable proxy for SPY (it *is* an ~30-day SPX IV index); for anything
else, build the series from that name's own chains.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# One year of trading days: the conventional IV-rank lookback.
DEFAULT_LOOKBACK_DAYS = 252


@dataclass
class IVRankReading:
    current_iv: float
    iv_rank: float | None          # 0..1, None when the series is unusable
    iv_percentile: float | None    # 0..1
    lookback_days: int
    low: float | None = None
    high: float | None = None

    def meets(self, minimum: float | None, use_percentile: bool = True) -> bool:
        """True when the preference is satisfied (or not configured).

        Fails OPEN, unlike the vol-regime gate: a missing IV history is a
        reason to lose a *preference*, not a reason to refuse a trade the
        rest of the pipeline approved. The vol-regime gate fails closed
        because it protects against a live crash; this one only expresses
        that richer premium is nicer to sell.
        """
        if minimum is None:
            return True
        value = self.iv_percentile if use_percentile else self.iv_rank
        if value is None:
            return True
        return value >= minimum

    def describe(self) -> str:
        def pct(v: float | None) -> str:
            return "n/a" if v is None else f"{v:.0%}"
        return (f"IV {self.current_iv:.1%} | rank {pct(self.iv_rank)} | "
                f"percentile {pct(self.iv_percentile)} "
                f"over {self.lookback_days}d")


def iv_rank(current_iv: float, history: pd.Series) -> float | None:
    """(current - min) / (max - min), clamped to [0, 1]. None if the series
    is empty or flat (a flat series has no rank -- returning 0 or 1 would
    both be arbitrary)."""
    s = pd.Series(history).dropna()
    if s.empty:
        return None
    low, high = float(s.min()), float(s.max())
    if high <= low:
        return None
    return min(1.0, max(0.0, (current_iv - low) / (high - low)))


def iv_percentile(current_iv: float, history: pd.Series) -> float | None:
    """Fraction of lookback observations strictly below current IV."""
    s = pd.Series(history).dropna()
    if s.empty:
        return None
    return float((s < current_iv).sum()) / float(len(s))


def read_iv_rank(current_iv: float, history: pd.Series,
                 lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> IVRankReading:
    """Both statistics over the trailing `lookback_days` of `history`
    (oldest first)."""
    s = pd.Series(history).dropna()
    s = s.iloc[-lookback_days:]
    return IVRankReading(
        current_iv=current_iv,
        iv_rank=iv_rank(current_iv, s),
        iv_percentile=iv_percentile(current_iv, s),
        lookback_days=len(s),
        low=float(s.min()) if not s.empty else None,
        high=float(s.max()) if not s.empty else None,
    )


def atm_iv(chain: pd.DataFrame, spot: float) -> float | None:
    """One day's at-the-money IV observation, averaging the nearest-strike
    put and call.

    Averaging the two sides matters: a single side's ATM IV carries the
    skew of that side, so a put-only series drifts with skew steepness
    rather than with the level of vol, and an IV rank built on it partly
    ranks skew. Falls back to whichever side exists.
    """
    if chain is None or chain.empty or "iv" not in chain.columns:
        return None
    usable = chain[(chain["iv"] > 0)]
    if usable.empty:
        return None
    sides = []
    for opt_type in ("put", "call"):
        side = usable[usable["type"] == opt_type]
        if side.empty:
            continue
        nearest = side.loc[(side["strike"] - spot).abs().idxmin()]
        sides.append(float(nearest["iv"]))
    if not sides:
        return None
    return sum(sides) / len(sides)


def build_iv_history(observations: dict[str, float]) -> pd.Series:
    """Date-keyed ATM IV observations -> a chronological Series.

    The pipeline accumulates these one scan at a time (see
    `scripts/scan_income.py --iv-history`), so the first year of running
    the strategy produces a short, growing lookback. `read_iv_rank` reports
    the actual `lookback_days` it used precisely so a 30-day-old series is
    never mistaken for a 252-day one.
    """
    if not observations:
        return pd.Series(dtype=float)
    s = pd.Series(observations, dtype=float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()
