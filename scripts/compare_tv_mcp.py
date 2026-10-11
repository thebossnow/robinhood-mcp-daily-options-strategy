#!/usr/bin/env python3
"""Compare TradingView MCP (Yahoo-backed) quotes/IV to Robinhood MCP.

    python scripts/compare_tv_mcp.py
    python scripts/compare_tv_mcp.py --symbol SPY --expiry 2026-09-04
    python scripts/compare_tv_mcp.py --symbols SPY QQQ IWM --json
    python scripts/compare_tv_mcp.py --skip-rh          # TV tape only

Prints a spot/VIX/ATM bid-ask-IV table. Flags IV when |RH-TV|/TV > 10%.
Does not place orders. TV options/quotes come from Yahoo Finance via the
tradingview-mcp-server package (`uv tool install tradingview-mcp-server`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.data.tv_compare import (
    IV_REL_WARN,
    format_report,
    report_to_dict,
    run_compare,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="TV MCP vs Robinhood MCP quote/IV diff (read-only)"
    )
    ap.add_argument("--symbol", help="Single underlying (default: SPY)")
    ap.add_argument("--symbols", nargs="+", help="Multiple underlyings")
    ap.add_argument("--expiry", help="YYYY-MM-DD; default = first in DTE window")
    ap.add_argument("--config", help="StrategyConfig JSON (DTE window, underlyings)")
    ap.add_argument("--min-dte", type=int, help="Override config min_dte")
    ap.add_argument("--max-dte", type=int, help="Override config max_dte")
    ap.add_argument("--iv-warn", type=float, default=IV_REL_WARN,
                    help="Relative IV gap that gets flagged (default 0.10)")
    ap.add_argument("--skip-rh", action="store_true",
                    help="Do not call Robinhood MCP")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="Machine-readable output")
    args = ap.parse_args()

    cfg = StrategyConfig.from_json(args.config) if args.config else StrategyConfig()
    if args.symbols:
        symbols = args.symbols
    elif args.symbol:
        symbols = [args.symbol]
    else:
        symbols = list(cfg.underlyings[:1]) or ["SPY"]

    min_dte = cfg.min_dte if args.min_dte is None else args.min_dte
    max_dte = cfg.max_dte if args.max_dte is None else args.max_dte

    reports = run_compare(
        symbols,
        expiry=args.expiry,
        min_dte=min_dte,
        max_dte=max_dte,
        iv_rel_warn=args.iv_warn,
        skip_rh=args.skip_rh,
    )

    if args.as_json:
        print(json.dumps([report_to_dict(r) for r in reports], indent=2))
    else:
        print("TradingView MCP vs Robinhood MCP (read-only)")
        print(f"DTE window {min_dte}-{max_dte}; IV warn > {args.iv_warn:.0%}")
        print()
        for r in reports:
            print(format_report(r))
            print()
        if any(leg.iv_warn for r in reports for leg in r.legs):
            print("IV mismatch: Robinhood vol is what credit-path BS delta uses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
