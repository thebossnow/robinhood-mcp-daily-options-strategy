"""Post-entry IV spike: what to do when vol explodes under a short position.

The framework's rule is "roll or close if implied volatility spikes sharply
after entry, like a jump from 15 to 40 percent". The instinct is right --
that is the regime where short premium goes from boring to dangerous -- but
"close on an IV spike" as a mechanical trigger inverts the trade at the
worst possible moment, for two reasons this repo can point at directly:

1. An IV spike is the moment your buy-back price is most inflated. Closing
   then pays the peak of the very premium you sold. Short vol positions are
   short an asset that mean-reverts; exiting at the top of a spike converts
   a temporary mark-to-market loss into a realized one at the worst price.

2. This repo already measured the closest analogue and it was the single
   largest loss driver. The 2022-26 SPY sweep found the short-strike breach
   stop -- also an "exit when it moves against you" rule -- fired on 46% of
   trades, ALL of them losers, averaging -$160, and removing it improved
   every configuration tested (README, "Validated variants"). Stop-on-touch
   turned a majority-winner structure into a majority-loser one. An
   IV-spike stop is the same shape of rule and deserves the same suspicion
   until it is measured, not the benefit of the doubt.

What actually distinguishes a survivable spike from a fatal one is not the
IV level, it is whether the short strike is now genuinely threatened. A VIX
15 -> 40 event with the underlying still 6% above the short put is a
position that will very likely expire worthless; the same IV move with spot
sitting at the strike is a different trade. So the trigger here is GRADED,
and it reads delta alongside IV:

    NONE     nothing unusual
    WATCH    IV up materially, short strike still far away -> hold, mark daily
    DEFEND   IV spiked AND short delta has grown past the defend threshold
             -> roll out/down for a credit, or close if no credit roll exists
    CLOSE    short delta through the breach threshold, or the roll budget is
             exhausted -> take the loss; this is a risk decision, not a
             vol decision

Rolling is capped by `max_rolls` because a roll is not a fix: it is a loss
deferred in exchange for more time and a little credit. Two rolls of a
tested short put is a position being managed; five is a position that has
been quietly converted into a directional bet nobody sized for.

Nothing in this module fires automatically in the live path. It produces a
signal and a reason; `scripts/manage_income.py` reports it, and a human
authorizes any defensive action -- matching the repo's rule that the agent
never opens or adjusts a position without confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass

NONE, WATCH, DEFEND, CLOSE = "NONE", "WATCH", "DEFEND", "CLOSE"


@dataclass
class IVSpikeConfig:
    # Current IV / entry IV. The framework's "15 to 40" example is 2.67x;
    # 1.5x is where a spike stops being noise on a 30-45 DTE index position.
    watch_iv_ratio: float = 1.5
    defend_iv_ratio: float = 2.0
    # Short-leg |delta| thresholds. Entry is 20-30 delta; delta rising
    # through 0.40 means the market now prices a materially higher chance
    # of finishing in the money than the trade was sold on.
    defend_delta: float = 0.40
    breach_delta: float = 0.50
    max_rolls: int = 2

    def to_dict(self) -> dict:
        return {"watch_iv_ratio": self.watch_iv_ratio,
                "defend_iv_ratio": self.defend_iv_ratio,
                "defend_delta": self.defend_delta,
                "breach_delta": self.breach_delta,
                "max_rolls": self.max_rolls}


@dataclass
class IVSpikeSignal:
    action: str
    reason: str
    iv_ratio: float | None = None
    short_delta: float | None = None
    rolls_used: int = 0

    @property
    def needs_attention(self) -> bool:
        return self.action in (DEFEND, CLOSE)

    def describe(self) -> str:
        bits = [self.action, self.reason]
        if self.iv_ratio is not None:
            bits.append(f"IV x{self.iv_ratio:.2f}")
        if self.short_delta is not None:
            bits.append(f"short delta {self.short_delta:.2f}")
        return " | ".join(bits)


def assess(entry_iv: float, current_iv: float, short_delta: float | None,
           cfg: IVSpikeConfig | None = None,
           rolls_used: int = 0) -> IVSpikeSignal:
    """Grade the current state of one short-premium position.

    `short_delta` is the |delta| of the tested short leg (the put side in a
    selloff), or None when the chain does not supply it -- in which case IV
    alone can raise a WATCH but never a DEFEND, because the piece of
    evidence that distinguishes the two is missing. Erring toward WATCH is
    deliberate: the expensive mistake in this module is a spurious exit at
    peak IV, not a delayed one.
    """
    cfg = cfg or IVSpikeConfig()
    ratio = (current_iv / entry_iv) if entry_iv > 0 else None
    d = abs(short_delta) if short_delta is not None else None

    if d is not None and d >= cfg.breach_delta:
        return IVSpikeSignal(
            CLOSE, f"short delta {d:.2f} at/through breach threshold "
                   f"{cfg.breach_delta:.2f} — the strike, not the vol, is "
                   f"the problem", ratio, d, rolls_used)

    if ratio is None:
        return IVSpikeSignal(NONE, "no entry IV recorded — cannot assess "
                                   "spike", None, d, rolls_used)

    if ratio >= cfg.defend_iv_ratio and d is not None and d >= cfg.defend_delta:
        if rolls_used >= cfg.max_rolls:
            return IVSpikeSignal(
                CLOSE, f"defend conditions met but {rolls_used} roll(s) "
                       f"already used (cap {cfg.max_rolls}) — further rolling "
                       f"defers loss without reducing it", ratio, d, rolls_used)
        return IVSpikeSignal(
            DEFEND, f"IV x{ratio:.2f} (>= {cfg.defend_iv_ratio:g}) with short "
                    f"delta {d:.2f} (>= {cfg.defend_delta:g}) — roll out/down "
                    f"for a credit, or close if no credit roll exists",
            ratio, d, rolls_used)

    if ratio >= cfg.watch_iv_ratio:
        far = "" if d is None else f", short delta still {d:.2f}"
        return IVSpikeSignal(
            WATCH, f"IV x{ratio:.2f} (>= {cfg.watch_iv_ratio:g}){far} — hold "
                   f"and mark daily; closing into peak IV pays the inflated "
                   f"premium you sold", ratio, d, rolls_used)

    return IVSpikeSignal(NONE, f"IV x{ratio:.2f} — within normal range",
                         ratio, d, rolls_used)
