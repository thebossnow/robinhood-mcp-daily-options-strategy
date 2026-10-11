#!/usr/bin/env python3
"""Compute ATR and ATM IV for SPY/QQQ/IWM and print the playbook bucket.

    python scripts/atr_iv.py
    python scripts/atr_iv.py --symbols SPY QQQ IWM --json

OHLC and ATM IV default to yfinance. Pass --iv-source mcp to take ATM IV
from Robinhood MCP (spot still from the OHLC close so ATR stays consistent).
Read-only: no orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.signals.atr_iv import (
    TARGET_PUT_DELTA,
    format_snapshot,
    snapshot_from_ohlc,
)
from options_trader.signals.credit import bs_delta
from options_trader.signals.math import TRADING_DAYS_PER_YEAR


def _yf():
    import yfinance as yf
    return yf


def fetch_ohlc(symbol: str, period: str = "6mo") -> pd.DataFrame:
    hist = _yf().Ticker(symbol).history(period=period, auto_adjust=False)
    if hist.empty or len(hist) < 30:
        raise RuntimeError(f"{symbol}: not enough OHLC from yfinance")
    return hist[["High", "Low", "Close"]].dropna()


def _pick_expiry(expirations: list[str], today: date, target_dte: int) -> str | None:
    scored = []
    for raw in expirations:
        try:
            exp = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        dte = (exp - today).days
        if dte < 1:
            continue
        scored.append((abs(dte - target_dte), dte, str(raw)[:10]))
    if not scored:
        return None
    scored.sort()
    return scored[0][2]


def atm_iv_from_chain(chain: pd.DataFrame, spot: float) -> tuple[float | None, float]:
    """Average of ATM call IV and ATM put IV; also returns the ATM strike."""
    if chain is None or chain.empty or spot <= 0:
        return None, float("nan")
    strike = min(chain["strike"].unique(), key=lambda k: abs(float(k) - spot))
    ivs = []
    for side in ("call", "put"):
        rows = chain[(chain["type"] == side) & (chain["strike"] == strike)]
        if rows.empty:
            continue
        iv = float(rows.iloc[0]["iv"] or 0)
        if 0 < iv < 3.0:
            ivs.append(iv)
        elif iv >= 3.0:
            ivs.append(iv / 100.0)
    if not ivs:
        return None, float(strike)
    return sum(ivs) / len(ivs), float(strike)


def fetch_atm_iv_yf(symbol: str, spot: float, target_dte: int = 30):
    t = _yf().Ticker(symbol)
    today = date.today()
    try:
        exps = list(t.options)
    except Exception as e:
        raise RuntimeError(f"{symbol}: yfinance expirations failed: {e}") from e
    expiry = _pick_expiry(exps, today, target_dte)
    if expiry is None:
        return None, None, None
    from options_trader.data.provider import YFinanceProvider
    snap = YFinanceProvider().get_chain(symbol, expiry)
    iv, _ = atm_iv_from_chain(snap.chain, spot)
    dte = (date.fromisoformat(expiry) - today).days
    return iv, dte, expiry


def fetch_atm_iv_mcp(symbol: str, spot: float, target_dte: int = 30):
    from options_trader.data.mcp_provider import MCPDataProvider
    p = MCPDataProvider()
    today = date.today()
    expiry = _pick_expiry(p.get_expirations(symbol), today, target_dte)
    if expiry is None:
        return None, None, None
    snap = p.get_chain(symbol, expiry)
    iv, _ = atm_iv_from_chain(snap.chain, float(snap.spot or spot))
    dte = (date.fromisoformat(expiry) - today).days
    return iv, dte, expiry


def strike_for_put_delta(spot: float, iv: float, dte: int,
                         target: float = TARGET_PUT_DELTA) -> float | None:
    """Strike whose BS put delta is about -target (0.15 → -0.15)."""
    if spot <= 0 or iv <= 0 or dte < 1:
        return None
    t_years = dte / TRADING_DAYS_PER_YEAR
    lo, hi = spot * 0.5, spot
    target_delta = -abs(target)
    for _ in range(40):
        mid = (lo + hi) / 2.0
        d = bs_delta("put", spot, mid, iv, t_years)
        if d > target_delta:   # too little |delta| → too far OTM → raise strike
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description="ATR + ATM IV for index ETFs")
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    ap.add_argument("--target-dte", type=int, default=30,
                    help="Pick the expiration closest to this DTE for ATM IV")
    ap.add_argument("--iv-source", choices=["yfinance", "mcp"], default="yfinance")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    rows = []
    for symbol in args.symbols:
        ohlc = fetch_ohlc(symbol)
        spot = float(ohlc["Close"].iloc[-1])
        if args.iv_source == "mcp":
            atm_iv, dte, expiry = fetch_atm_iv_mcp(symbol, spot, args.target_dte)
        else:
            atm_iv, dte, expiry = fetch_atm_iv_yf(symbol, spot, args.target_dte)
        put_k = None
        if atm_iv and dte:
            put_k = strike_for_put_delta(spot, atm_iv, dte)
        snap = snapshot_from_ohlc(
            symbol, ohlc, atm_iv=atm_iv, iv_dte=dte, iv_expiry=expiry,
            put_015_delta_strike=put_k,
        )
        rows.append(snap)

    if args.as_json:
        print(json.dumps([r.to_dict() for r in rows], indent=2, default=str))
    else:
        print("ATR vs ATM IV  (yfinance OHLC; "
              f"IV from {args.iv_source}, ~{args.target_dte} DTE)")
        print("ATR/implied > 1 → recent ranges exceed the option market's "
              "daily move (IV looks cheap vs ATR).")
        print()
        for r in rows:
            print(format_snapshot(r))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
