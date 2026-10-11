"""Unit tests for TV vs Robinhood quote/IV comparison. No network."""

from datetime import date

import pandas as pd
import pytest

from options_trader.data.tv_compare import (
    compare_symbol,
    diff_leg,
    format_report,
    nearest_shared_strike,
    normalize_iv,
    pick_expiry,
    report_to_dict,
    run_compare,
    strike_key,
)


class TestNormalizeIv:
    def test_decimal_passthrough(self):
        iv, suspect = normalize_iv(0.1108)
        assert iv == pytest.approx(0.1108)
        assert not suspect

    def test_percent_rescaled(self):
        iv, suspect = normalize_iv(11.08)
        assert iv == pytest.approx(0.1108)
        assert suspect

    def test_none(self):
        assert normalize_iv(None) == (None, False)


class TestPickExpiry:
    def test_prefers_low_end_of_window(self):
        today = date(2026, 8, 31)
        exps = ["2026-09-01", "2026-09-02", "2026-09-08", "2026-09-15"]
        # min_dte=1 → Sep 1 is 1 DTE
        assert pick_expiry(exps, today, 1, 7) == "2026-09-01"

    def test_skips_0dte_when_min_dte_is_1(self):
        today = date(2026, 9, 1)
        exps = ["2026-09-01", "2026-09-02", "2026-09-04"]
        assert pick_expiry(exps, today, 1, 7) == "2026-09-02"

    def test_empty_when_nothing_in_window(self):
        today = date(2026, 8, 31)
        assert pick_expiry(["2026-10-16"], today, 1, 7) is None


class TestSharedStrike:
    def test_picks_closest_to_spot(self):
        assert nearest_shared_strike(
            [766.0, 767.0, 768.0], [767.0, 768.0, 900.0], 767.05
        ) == 767.0

    def test_none_when_no_overlap(self):
        assert nearest_shared_strike([100.0], [200.0], 100.0) is None

    def test_float_rounding(self):
        assert strike_key(767.0000001) == 767.0


class TestDiffLeg:
    def test_iv_warn_when_gap_exceeds_threshold(self):
        tv = {"bid": 1.79, "ask": 1.80, "volume": 10, "open_interest": 10,
              "iv": 0.1108}
        rh = {"bid": 1.79, "ask": 1.80, "volume": 10, "open_interest": 10,
              "iv": 0.1334}
        leg = diff_leg("call", 767.0, tv, rh, iv_rel_warn=0.10)
        assert leg.iv_warn
        assert leg.iv_rel_diff == pytest.approx((0.1334 - 0.1108) / 0.1108)
        assert not leg.iv_unit_suspect

    def test_matching_quotes_no_warn(self):
        row = {"bid": 1.42, "ask": 1.44, "volume": 1, "open_interest": 1,
               "iv": 0.09}
        leg = diff_leg("put", 767.0, row, row)
        assert not leg.iv_warn
        assert leg.iv_rel_diff == pytest.approx(0.0)


class TestCompareSymbol:
    def test_atm_table_from_both_tapes(self):
        tv_chain = {
            "requested_expiry": "2026-09-02",
            "calls": [
                {"strike": 767.0, "bid": 1.79, "ask": 1.80, "volume": 89233,
                 "open_interest": 1266, "implied_volatility": 0.1108},
            ],
            "puts": [
                {"strike": 767.0, "bid": 1.42, "ask": 1.44, "volume": 47337,
                 "open_interest": 1315, "implied_volatility": 0.0914},
            ],
        }
        rh_chain = pd.DataFrame([
            {"type": "call", "strike": 767.0, "bid": 1.79, "ask": 1.80,
             "volume": 89230, "open_interest": 1266, "iv": 0.1334},
            {"type": "put", "strike": 767.0, "bid": 1.42, "ask": 1.44,
             "volume": 47336, "open_interest": 1315, "iv": 0.1065},
        ])
        report = compare_symbol(
            symbol="SPY", expiry="2026-09-02",
            tv_spot=767.05, rh_spot=766.95,
            tv_vix=14.92, rh_vix=14.92,
            tv_chain=tv_chain, rh_chain=rh_chain,
            vol_gate_allowed=True, vol_gate_reason=None,
        )
        assert report.atm_strike == 767.0
        assert report.spot_diff == pytest.approx(-0.10)
        assert report.vix_diff == pytest.approx(0.0)
        assert report.vol_gate_allowed is True
        sides = {leg.side: leg for leg in report.legs}
        assert sides["call"].iv_warn
        assert sides["put"].iv_warn
        assert sides["call"].tv_bid == 1.79
        assert sides["call"].rh_bid == 1.79
        text = format_report(report)
        assert "SPY" in text
        assert "IV" in text
        payload = report_to_dict(report)
        assert payload["symbol"] == "SPY"
        assert len(payload["legs"]) == 2


class TestRunCompareOrchestration:
    def test_refetches_tv_when_rh_picks_a_later_expiry(self):
        calls = []

        def fake_tv(symbol, expiry):
            calls.append(("tv", expiry))
            if expiry is None:
                return {
                    "spot": 767.05, "vix": 14.92,
                    "available_expiries": ["2026-09-01", "2026-09-02"],
                    "chain": {
                        "requested_expiry": "2026-09-01",
                        "calls": [{"strike": 767.0, "bid": 1.0, "ask": 1.1,
                                   "implied_volatility": 0.2}],
                        "puts": [{"strike": 767.0, "bid": 1.0, "ask": 1.1,
                                  "implied_volatility": 0.2}],
                    },
                }
            return {
                "spot": 767.05, "vix": 14.92,
                "available_expiries": ["2026-09-01", "2026-09-02"],
                "chain": {
                    "requested_expiry": expiry,
                    "calls": [{"strike": 767.0, "bid": 2.0, "ask": 2.1,
                               "implied_volatility": 0.11}],
                    "puts": [{"strike": 767.0, "bid": 2.0, "ask": 2.1,
                              "implied_volatility": 0.11}],
                },
            }

        def fake_rh(symbol, expiry, min_dte, max_dte, today):
            calls.append(("rh", expiry))
            return {
                "spot": 766.95, "vix": 14.92, "expiry": "2026-09-02",
                "chain": pd.DataFrame([
                    {"type": "call", "strike": 767.0, "bid": 2.0, "ask": 2.1,
                     "volume": 1, "open_interest": 1, "iv": 0.11},
                    {"type": "put", "strike": 767.0, "bid": 2.0, "ask": 2.1,
                     "volume": 1, "open_interest": 1, "iv": 0.11},
                ]),
                "vol_gate_allowed": True, "vol_gate_reason": None, "notes": [],
            }

        reports = run_compare(
            ["SPY"], today=date(2026, 8, 31),
            fetch_tv_fn=fake_tv, fetch_rh_fn=fake_rh,
        )
        assert ("tv", None) in calls
        assert ("tv", "2026-09-02") in calls
        assert reports[0].expiry == "2026-09-02"
        assert reports[0].legs[0].tv_bid == 2.0

    def test_skip_rh(self):
        def fake_tv(symbol, expiry):
            return {
                "spot": 100.0, "vix": 15.0,
                "available_expiries": ["2026-09-02"],
                "chain": {
                    "requested_expiry": "2026-09-02",
                    "calls": [{"strike": 100.0, "bid": 1.0, "ask": 1.1,
                               "implied_volatility": 0.2}],
                    "puts": [],
                },
            }

        def boom(*_a, **_k):
            raise AssertionError("RH must not be called")

        reports = run_compare(
            ["SPY"], expiry="2026-09-02", skip_rh=True,
            fetch_tv_fn=fake_tv, fetch_rh_fn=boom,
        )
        assert reports[0].rh_spot is None
        assert "RH skipped" in reports[0].notes
