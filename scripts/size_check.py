#!/usr/bin/env python3
"""Answer "can my account actually trade this?" before any data is fetched.

    python scripts/size_check.py --equity 500 --spot 762.60
    python scripts/size_check.py --equity 25000 --spot 762.60 --width 5

The income framework's sizing schedule (10% / 7% / 5% by account size) is
easy to read as generous. Applied to its own instruction -- "size every
position so a full assignment or max loss stays within that limit" -- it is
the opposite, and the gap between the two readings is several orders of
magnitude. This tool prints the arithmetic so the conclusion is checkable
rather than asserted.

It needs no market data beyond a spot price: max loss on a defined-risk
spread is (width - credit), and a cash-secured put's collateral is
(strike - credit) * 100. Credits are entered as a fraction of width (or of
strike) so the answer does not depend on a live chain -- and the ranges
that matter are wide enough that the conclusion is robust to the credit
assumption anyway.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.risk.sizing import (
    DEFAULT_TIERS, csp_capital_at_risk, min_equity_for_one_contract,
    size_position, spread_capital_at_risk, tier_for,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, required=True)
    ap.add_argument("--spot", type=float, required=True,
                    help="Underlying price, e.g. 762.60 for SPY")
    ap.add_argument("--width", type=float, default=1.0,
                    help="Defined-risk spread width in points (default 1)")
    ap.add_argument("--credit-frac", type=float, default=0.20,
                    help="Credit as a fraction of width (default 0.20, the "
                         "middle of what SPY chains actually pay at these "
                         "deltas — the framework's 1/3 is not attainable)")
    ap.add_argument("--otm-pct", type=float, default=0.05,
                    help="How far OTM the short put sits, as a fraction of "
                         "spot (default 0.05, roughly a 20-25 delta strike "
                         "at 30-45 DTE on an index)")
    ap.add_argument("--heat-cap", type=float, default=None,
                    help="Optional book-level cap as a fraction of equity")
    args = ap.parse_args()

    tier = tier_for(args.equity)
    budget = args.equity * tier.risk_pct
    strike = args.spot * (1.0 - args.otm_pct)
    credit = args.credit_frac * args.width
    # Credit on a CSP is not a fraction of width; approximate it from the
    # spread credit scaled by nothing — a CSP's credit is small relative to
    # its strike, and the collateral is dominated by the strike either way.
    csp_credit = credit

    spread_risk = spread_capital_at_risk(args.width, credit)
    csp_risk = csp_capital_at_risk(strike, csp_credit)

    print(f"equity ${args.equity:,.2f} -> tier {tier.risk_pct:.0%} "
          f"({tier.label()}), budget ${budget:,.2f} per position\n")

    rows = [
        (f"defined-risk spread, {args.width:g}-pt wide", spread_risk),
        (f"cash-secured put, {strike:.2f} strike "
         f"({args.otm_pct:.0%} OTM of {args.spot:.2f})", csp_risk),
    ]
    for label, risk in rows:
        d = size_position(args.equity, risk, DEFAULT_TIERS,
                          portfolio_heat_cap_pct=args.heat_cap)
        need = min_equity_for_one_contract(risk, DEFAULT_TIERS)
        print(f"{label}")
        print(f"  capital at risk per contract : ${risk:,.2f}")
        print(f"  fits this account            : "
              f"{'YES — ' + d.summary() if d.feasible else 'NO'}")
        print(f"  equity needed for 1 contract : ${need:,.2f}")
        if not d.feasible:
            print(f"  reason                       : {d.reasons[0]}")
        print()

    ratio = csp_risk / spread_risk if spread_risk > 0 else float("inf")
    print(f"A cash-secured put here commits {ratio:,.0f}x the capital of the "
          f"{args.width:g}-point spread.\nThe framework applies one "
          f"percentage schedule to both.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
