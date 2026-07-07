#!/usr/bin/env python3
"""Daily scan: fetch chains, generate EV-positive vertical candidates,
save the scan for paper trading and the snapshot for backtesting.

    python scripts/scan.py                     # scan with default config
    python scripts/scan.py --save-snapshot     # also store chains for backtest
    python scripts/scan.py --config my.json    # custom StrategyConfig

Output: a human-readable report plus runs/scan_<timestamp>.json that
scripts/paper_trade.py can open positions from.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.data import YFinanceProvider, SnapshotStore
from options_trader.signals import generate_candidates


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="Path to StrategyConfig JSON")
    ap.add_argument("--save-snapshot", action="store_true",
                    help="Persist chains to data_snapshots/ for backtesting")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--provider", choices=["mcp", "yfinance"], default="yfinance",
                    help="Data source: mcp (Robinhood live) or yfinance (free fallback)")
    args = ap.parse_args()

    cfg = StrategyConfig.from_json(args.config) if args.config else StrategyConfig()
    if args.provider == "mcp":
        from options_trader.data import MCPDataProvider
        provider = MCPDataProvider()
        print("Using Robinhood MCP (live data)")
    else:
        provider = YFinanceProvider()
    store = SnapshotStore()

    all_candidates = []
    print(f"Scan {datetime.now().isoformat(timespec='minutes')} — "
          f"underlyings: {', '.join(cfg.underlyings)}, DTE {cfg.min_dte}-{cfg.max_dte}")

    for und in cfg.underlyings:
        try:
            expirations = provider.get_expirations(und)
        except Exception as e:
            print(f"  {und}: failed to fetch expirations: {e}")
            continue
        today = date.today()
        in_window = [
            e for e in expirations
            if cfg.min_dte <= (date.fromisoformat(e) - today).days <= cfg.max_dte
        ]
        for exp in in_window:
            try:
                snap = provider.get_chain(und, exp)
            except Exception as e:
                print(f"  {und} {exp}: chain fetch failed: {e}")
                continue
            if args.save_snapshot:
                store.save(snap)
            cands = generate_candidates(snap, cfg)
            print(f"  {und} {exp} (spot {snap.spot:.2f}): "
                  f"{len(cands)} candidates pass all filters")
            all_candidates.extend(cands)

    all_candidates.sort(key=lambda c: c.ev_after_costs, reverse=True)
    top = all_candidates[: cfg.top_n]

    if not top:
        print("\nNO QUALIFYING TRADE TODAY — nothing passed liquidity, "
              "structure, probability and after-cost EV filters.")
        return 0

    print(f"\nTop {len(top)} by EV after costs (per contract):")
    for i, c in enumerate(top):
        print(f"\n[{i}] {c.order_description()}")
        print(f"    max loss ${c.max_loss:.0f} | max profit ${c.max_profit:.0f} "
              f"| breakeven {c.breakeven:.2f}")
        print(f"    P(max profit) {c.p_win:.1%} | P(max loss) {c.p_loss:.1%} "
              f"| EV ${c.ev:.2f} | est. costs ${c.est_costs:.2f} "
              f"| EV after costs ${c.ev_after_costs:.2f}")

    runs = Path(args.runs_dir)
    runs.mkdir(exist_ok=True)
    out = runs / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps([c.to_dict() for c in top], indent=2))
    print(f"\nSaved to {out} — open a paper position with:")
    print(f"  python scripts/paper_trade.py open --scan-file {out} --index 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
