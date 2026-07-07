#!/usr/bin/env python3
"""Daily options scanner.

Screens SPY/QQQ/IWM near-dated chains for single-leg debit trades that
pass liquidity, expectancy, and sizing rules (see strategy_math.py).

Data source is yfinance (delayed). This is the *screening* pass: the
MCP-connected agent re-verifies every candidate against live Robinhood
quotes (get_option_chains -> get_option_instruments -> get_option_quotes)
before anything is recommended or placed. See AGENT_PROMPT.md.

Run: python scripts/options_scanner.py --account-value 500
Requires: pip install -r requirements.txt
"""

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone

import yfinance as yf

from strategy_math import LiquidityRules, build_trade_plan, expected_move, mid_price

UNDERLYINGS = ["SPY", "QQQ", "IWM"]
MAX_EXPIRATIONS = 3  # nearest expirations to scan


def _spot_price(ticker: yf.Ticker) -> float:
    info = ticker.fast_info
    return float(info.get("last_price") or info.get("lastPrice") or 0)


def _days_to_expiry(expiration: str) -> float:
    exp = datetime.strptime(expiration, "%Y-%m-%d").replace(
        hour=16, tzinfo=timezone.utc)
    return max((exp - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.1)


def _atm_expected_move(calls, puts, spot: float) -> float:
    """1-sigma expected move from the ATM straddle."""
    atm_call = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
    atm_put = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]
    call_mid = mid_price(float(atm_call["bid"].iloc[0]), float(atm_call["ask"].iloc[0]))
    put_mid = mid_price(float(atm_put["bid"].iloc[0]), float(atm_put["ask"].iloc[0]))
    return expected_move(spot, call_mid, put_mid)


def scan_expiration(symbol: str, ticker: yf.Ticker, expiration: str,
                    spot: float, account_value: float, risk_pct: float,
                    rules: LiquidityRules):
    chain = ticker.option_chain(expiration)
    dte = _days_to_expiry(expiration)
    em = _atm_expected_move(chain.calls, chain.puts, spot)
    plans = []
    for df, opt_type in ((chain.calls, "call"), (chain.puts, "put")):
        for _, row in df.iterrows():
            strike = float(row["strike"])
            # Only consider strikes within one expected move: beyond that,
            # the "2x" target needs a move the market prices as unlikely.
            if em > 0 and abs(strike - spot) > em:
                continue
            plan = build_trade_plan(
                ticker=symbol, expiration=expiration, option_type=opt_type,
                strike=strike,
                bid=float(row.get("bid") or 0), ask=float(row.get("ask") or 0),
                iv=float(row.get("impliedVolatility") or 0), spot=spot,
                days_to_expiry=dte,
                open_interest=int(row.get("openInterest") or 0),
                volume=int(row.get("volume") or 0),
                account_value=account_value, risk_pct=risk_pct, rules=rules,
            )
            if plan:
                plans.append(plan)
    return plans


def main():
    parser = argparse.ArgumentParser(description="Scan for qualifying daily options trades")
    parser.add_argument("--account-value", type=float, required=True,
                        help="Current account value in dollars (sizing input)")
    parser.add_argument("--risk-pct", type=float, default=0.02,
                        help="Max fraction of account risked per trade (default 0.02)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    rules = LiquidityRules()
    all_plans = []
    for symbol in UNDERLYINGS:
        ticker = yf.Ticker(symbol)
        spot = _spot_price(ticker)
        if spot <= 0:
            print(f"# {symbol}: no spot price, skipping")
            continue
        for expiration in ticker.options[:MAX_EXPIRATIONS]:
            try:
                all_plans.extend(scan_expiration(
                    symbol, ticker, expiration, spot,
                    args.account_value, args.risk_pct, rules))
            except Exception as exc:  # network/data hiccups shouldn't kill the scan
                print(f"# {symbol} {expiration}: {exc}")

    all_plans.sort(key=lambda p: p.ev_per_contract, reverse=True)

    if args.json:
        print(json.dumps([asdict(p) for p in all_plans], indent=2))
        return

    if not all_plans:
        print("NO QUALIFYING TRADE TODAY — nothing passed liquidity + expectancy + sizing.")
        return
    print(f"{len(all_plans)} candidate(s), best first. Verify against live MCP quotes before trading.\n")
    for p in all_plans[:10]:
        print(f"{p.ticker} {p.expiration} {p.strike:g} {p.option_type.upper()}"
              f" @ ~{p.entry_mid:.2f} mid | debit ${p.debit:.0f}/ct"
              f" | target {p.profit_target:.2f} / stop {p.stop_loss:.2f}"
              f" | p(win)~{p.p_win:.0%} EV ${p.ev_per_contract:.2f}/ct"
              f" | size {p.contracts} ct | OI {p.open_interest} vol {p.volume}"
              f" spread {p.spread:.0%}")


if __name__ == "__main__":
    main()
