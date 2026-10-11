#!/usr/bin/env python3
"""Weekly put-credit using 15-delta, pushed to 2×ATR if delta is too close.

    python scripts/scan_weekly_atr.py                 # print only
    python scripts/scan_weekly_atr.py --provider mcp  # live Robinhood chain
    python scripts/scan_weekly_atr.py --open          # paper-journal a fill

Does not place live orders. Same RiskManager gate as scan_credit.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.journal import Journal
from options_trader.risk.vol_regime import fetch_vix_closes
from options_trader.signals.atr_iv import fetch_atr14
from options_trader.signals.credit import (
    VALIDATED, atr_short_bound, build_position, leg_passes_live_liquidity,
    _pick_short, _with_deltas,
)


def pick_expiration(expirations: list[str], today: date, min_dte: int,
                    max_dte: int, target_dte: int) -> str | None:
    def dte(e: str) -> int:
        return (date.fromisoformat(e) - today).days
    ok = [e for e in expirations if min_dte <= dte(e) <= max_dte]
    if not ok:
        return None
    return min(ok, key=lambda e: abs(dte(e) - target_dte))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/credit_paper.json")
    ap.add_argument("--provider", choices=["mcp", "yfinance"], default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--open", action="store_true",
                    help="Record a paper fill (default is print-only)")
    args = ap.parse_args()

    vcfg = VALIDATED["spy_weekly_atr15"]
    today = date.today()
    cfg = StrategyConfig.from_json(args.config) if Path(args.config).exists() \
        else StrategyConfig(underlyings=[args.symbol])

    if args.provider == "mcp":
        from options_trader.data.mcp_provider import MCPDataProvider
        provider = MCPDataProvider()
    else:
        from options_trader.data.provider import YFinanceProvider
        provider = YFinanceProvider()

    atr = fetch_atr14(args.symbol)
    if atr is None:
        print(f"{args.symbol}: ATR-14 unavailable — aborting")
        return 1

    expirations = provider.get_expirations(args.symbol)
    expiration = pick_expiration(
        expirations, today, vcfg.min_dte, vcfg.max_dte, vcfg.target_dte,
    )
    if expiration is None:
        print(f"{args.symbol}: no expiration in {vcfg.min_dte}-{vcfg.max_dte} DTE")
        return 0

    snap = provider.get_chain(args.symbol, expiration)
    liquid = snap.chain[snap.chain.apply(leg_passes_live_liquidity, axis=1)]
    bound = atr_short_bound(snap.spot, atr, vcfg.atr_mult, "put")

    puts = _with_deltas(
        liquid[liquid["type"] == "put"], "put", snap.spot, max(snap.dte, 1) / 365.0,
    )
    delta_short = _pick_short(puts, vcfg.short_put_delta, vcfg.min_short_bid)
    delta_k = None if delta_short is None else float(delta_short["strike"])
    delta_d = None if delta_short is None else float(delta_short["abs_delta"])

    pos = build_position(
        liquid, snap.spot, args.symbol, today.isoformat(), expiration,
        snap.dte, vcfg, cfg.slippage_half_spread_frac, atr=atr,
    )

    print(f"{args.symbol} {expiration} ({snap.dte} DTE)  spot {snap.spot:.2f}")
    print(f"  ATR-14 ${atr:.2f}  {vcfg.atr_mult:g}×ATR put floor {bound:.2f}")
    if delta_k is not None:
        where = "INSIDE band → push OTM" if delta_k > bound else "already outside band"
        print(f"  15Δ put strike {delta_k:g} (abs Δ {delta_d:.3f})  {where}")
    if pos is None:
        print("  NO QUALIFYING TRADE")
        return 0
    short = [l for l in pos.legs if l.side < 0][0]
    wing = [l for l in pos.legs if l.side > 0][0]
    print(f"  chosen short {short.strike:g}P / wing {wing.strike:g}P")
    print(f"  credit {pos.credit:.2f}  max loss ${pos.max_loss * 100:.0f}/contract")

    if not args.open:
        print("  (print-only; pass --open to paper-journal)")
        return 0

    broker = PaperBroker(
        cfg, Journal(args.journal),
        vix_provider=lambda: fetch_vix_closes(cfg.vix_spike_lookback_days),
    )
    trade_id, check = broker.open_credit(
        pos, contracts=1,
        notes=f"paper entry {vcfg.name} atr={atr:.2f} bound={bound:.2f}",
    )
    if trade_id is None:
        print("  REFUSED:", "; ".join(check.reasons))
        return 1
    print(f"  OPENED paper #{trade_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
