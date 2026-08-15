#!/usr/bin/env python3
"""Live smoke test for the EODHD wiring. Run this on a machine with network
access to eodhd.com:

    export EODHD_API_KEY=your_key_here
    python scripts/check_eodhd.py            # defaults to SPY
    python scripts/check_eodhd.py --symbol QQQ

It answers the three questions the unit tests cannot, because they mock HTTP:

1. Does the base plan work (spot price)?
2. Does this key have the UnicornBay options add-on, or does it 402/403?
3. Do the real response fields match what the provider expects — in
   particular, are volume and open interest actually populated?

Nothing is written to disk and no orders are placed; this is read-only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.data.eodhd import (  # noqa: E402
    CONTRACTS_PATH,
    EODHDClient,
    EODHDError,
    EODHDProvider,
    EODHDSubscriptionError,
)

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--max-dte", type=int, default=60)
    args = ap.parse_args()

    try:
        client = EODHDClient()
    except EODHDError as e:
        print(f"[{BAD}] {e}")
        return 1

    print(f"Checking EODHD for {args.symbol} (DTE 0-{args.max_dte})\n")

    # 1. Base plan: spot price. Costs 1 API call.
    provider = EODHDProvider(client=client, min_dte=0, max_dte=args.max_dte)
    try:
        spot = provider.get_spot(args.symbol)
        print(f"[{OK}] base plan: spot {args.symbol} = {spot:.2f}")
    except Exception as e:
        print(f"[{BAD}] base plan quote failed: {e}")
        return 1

    # 2. Options add-on. Costs 10 API calls per request.
    print(f"\nRequesting {CONTRACTS_PATH} (bills ~10 API calls)...")
    try:
        expirations = provider.get_expirations(args.symbol)
    except EODHDSubscriptionError as e:
        print(f"[{BAD}] options add-on NOT available on this key:\n       {e}")
        print("\n       The base plan works, so EOD underlying data is usable,")
        print("       but option chains need the paid UnicornBay SKU.")
        return 2
    except EODHDError as e:
        print(f"[{BAD}] options request failed: {e}")
        return 1

    print(f"[{OK}] options add-on active — {len(expirations)} expirations: "
          f"{expirations[:6]}{' ...' if len(expirations) > 6 else ''}")

    # 3. Field fidelity on a real chain — the reason to prefer this over DoltHub.
    try:
        snap = provider.get_chain(args.symbol, expirations[0])
    except EODHDError as e:
        print(f"[{BAD}] chain assembly failed: {e}")
        return 1

    chain = snap.chain
    print(f"\n[{OK}] chain {args.symbol} {snap.expiration}: {len(chain)} rows, "
          f"spot {snap.spot:.2f}, DTE {snap.dte}")
    print(chain.head(6).to_string(index=False))

    status = 0
    for col, label in [("volume", "volume"), ("open_interest", "open interest"),
                       ("iv", "implied vol")]:
        nonzero = int((chain[col] > 0).sum())
        pct = 100.0 * nonzero / max(len(chain), 1)
        mark = OK if pct > 20 else WARN
        if pct <= 20:
            status = 3
        print(f"[{mark}] {label}: {nonzero}/{len(chain)} rows populated ({pct:.0f}%)")

    live_bid = int((chain["bid"] > 0).sum())
    print(f"[{OK if live_bid else WARN}] live bid: {live_bid}/{len(chain)} rows")

    print(f"\nEstimated API calls billed this run: ~{client.calls_billed}")
    if status == 3:
        print("\nWARN: sparse volume/OI would starve the liquidity filter. "
              "Check whether the vendor populates these outside market hours "
              "before trusting a backtest built on them.")
    else:
        print("\nAll good — the provider is wired correctly against live data.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
