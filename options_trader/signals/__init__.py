# Light imports (no heavy deps)
from .probability import prob_above, prob_below

# Lazy imports for modules with heavier dependencies (pandas etc.)
# This mirrors the pattern used in options_trader/data/__init__.py
def __getattr__(name):
    if name in ("SpreadCandidate", "generate_candidates", "leg_passes_liquidity"):
        from .candidates import SpreadCandidate, generate_candidates, leg_passes_liquidity as _lpl
        globals().update({
            "SpreadCandidate": SpreadCandidate,
            "generate_candidates": generate_candidates,
            "leg_passes_liquidity": _lpl,
        })
        return globals()[name]
    if name in (
        "LiquidityRules", "SingleLegTradePlan", "build_single_leg_plan",
        "expected_move", "mid_price", "passes_liquidity",
        "prob_itm", "prob_touch", "spread_pct", "trade_ev",
    ):
        from .math import (
            LiquidityRules, SingleLegTradePlan, build_single_leg_plan,
            expected_move, mid_price, passes_liquidity,
            prob_itm, prob_touch, spread_pct, trade_ev,
        )
        globals().update({
            "LiquidityRules": LiquidityRules,
            "SingleLegTradePlan": SingleLegTradePlan,
            "build_single_leg_plan": build_single_leg_plan,
            "expected_move": expected_move,
            "mid_price": mid_price,
            "passes_liquidity": passes_liquidity,
            "prob_itm": prob_itm,
            "prob_touch": prob_touch,
            "spread_pct": spread_pct,
            "trade_ev": trade_ev,
        })
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "prob_above",
    "prob_below",
    "SpreadCandidate",
    "generate_candidates",
    "leg_passes_liquidity",
    # PR#3-derived shared math (usable for verticals + single-leg fallback)
    "LiquidityRules",
    "SingleLegTradePlan",
    "build_single_leg_plan",
    "expected_move",
    "mid_price",
    "passes_liquidity",
    "prob_itm",
    "prob_touch",
    "spread_pct",
    "trade_ev",
]
