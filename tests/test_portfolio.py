"""Portfolio orchestrator: position caps, satellite drawdown halt,
stop-out sit-out, and the wheel's full assign -> covered-call ->
called-away cycle end to end. Uses a FakeHistory so no network is touched.
"""

import pandas as pd
import pytest

from options_trader.backtest.portfolio import (
    OpenStructure, PortfolioBacktestEngine, PortfolioConfig, Sleeve,
    size_contracts,
)
from options_trader.signals.credit import CreditVariantConfig
from options_trader.signals.wheel import WheelConfig

EXP1 = "2026-02-13"   # 39 DTE from 2026-01-05
EXP2 = "2026-03-20"   # next cycle's expiration


def make_chain(expiration: str, spot: float = 100.0) -> pd.DataFrame:
    rows = []
    for strike in range(70, 131):
        k = float(strike)
        call_delta = max(0.01, min(0.99, 0.5 + (spot - k) * 0.04))
        put_delta = call_delta - 1.0
        for opt_type, delta in (("call", call_delta), ("put", put_delta)):
            mid = max(0.10, 2.0 * min(call_delta, 1 - call_delta) + 1.0)
            rows.append({"expiration": expiration, "type": opt_type,
                         "strike": k, "bid": round(mid - 0.05, 2),
                         "ask": round(mid + 0.05, 2), "iv": 0.25,
                         "delta": round(delta, 4)})
    return pd.DataFrame(rows)


def trading_days(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start=start, end=end)]


class FakeMultiHistory:
    """Serves a fixed chain per (symbol, expiration) pair. Optionally
    scales one symbol's mids by a per-day factor (>1 = costlier to close,
    i.e. an unrealized loss), for tests that need to force a real loss
    rather than just move spot."""

    def __init__(self, chains: dict[tuple[str, str], pd.DataFrame],
                value_factors: dict[str, dict[str, float]] | None = None):
        self.chains = chains
        self.value_factors = value_factors or {}

    def day_chains(self, symbol, day, max_dte=50):
        frames = [c for (s, e), c in self.chains.items() if s == symbol]
        if not frames:
            return pd.DataFrame(columns=["expiration", "type", "strike",
                                         "bid", "ask", "iv", "delta"])
        chain = pd.concat(frames, ignore_index=True).copy()
        factor = self.value_factors.get(symbol, {}).get(day, 1.0)
        if factor != 1.0:
            mid = (chain["bid"] + chain["ask"]) / 2.0 * factor
            chain["bid"] = (mid - 0.05).clip(lower=0.0)
            chain["ask"] = mid + 0.05
        return chain


class TestSizing:
    def test_risk_cap_limits_contracts(self):
        n = size_contracts(equity=10_000, max_loss_per_contract=150.0,
                           capital_required_per_contract=150.0,
                           cfg=PortfolioConfig())
        # 2% of 10k = 200 risk dollars / 150 per contract = 1
        assert n == 1

    def test_allocation_cap_can_bind_tighter_than_risk(self):
        cfg = PortfolioConfig(max_risk_per_trade_pct=0.50, max_ticker_alloc_pct=0.01)
        n = size_contracts(equity=10_000, max_loss_per_contract=10.0,
                           capital_required_per_contract=10.0, cfg=cfg)
        # risk cap alone would allow 500 contracts; 1% of equity / 10 = 10
        assert n == 10

    def test_zero_max_loss_returns_zero(self):
        assert size_contracts(10_000, 0.0, 0.0, PortfolioConfig()) == 0


class TestPositionCaps:
    def test_capped_run_lets_second_ticker_in_only_after_first_closes(self):
        chains = {
            ("F", EXP1): make_chain(EXP1, spot=100.0),
            ("AAL", EXP1): make_chain(EXP1, spot=100.0),
        }
        history = FakeMultiHistory(chains)
        days = trading_days("2026-01-05", "2026-02-13")
        spots = {(sym, d): 100.0 for sym in ("F", "AAL") for d in days}
        # exit_on_breach off, flat spot, no time stop: F's position
        # (entered first, sleeve ticker order) rides to expiry undisturbed.
        # AAL can only ever get a slot if F's structure closes first.
        credit_cfg = CreditVariantConfig(
            name="test_s2", short_put_delta=0.25, min_dte=30, max_dte=45,
            target_dte=39, min_credit_frac=0.0, exit_on_breach=False,
            time_exit_dte=0, time_exit_frac=0.0)
        sleeve = Sleeve(sleeve_id="S2", tickers=["F", "AAL"], is_satellite=False,
                        kind="credit", credit_cfg=credit_cfg)
        capped = PortfolioBacktestEngine(
            history, spots, PortfolioConfig(max_open_structures=1)
        ).run([sleeve], days[0], days[-1])
        assert {t.underlying for t in capped.trades} == {"F"}

        uncapped = PortfolioBacktestEngine(
            history, spots, PortfolioConfig(max_open_structures=4)
        ).run([sleeve], days[0], days[-1])
        assert {t.underlying for t in uncapped.trades} == {"F", "AAL"}


class TestSatelliteDrawdownHalt:
    def test_blocks_new_satellite_entry_after_a_realized_loss(self):
        # F (non-satellite, S2) blows through its hard stop on day 2,
        # realizing a loss that drags equity below peak. GME (satellite,
        # S3) only ever tries to enter starting day 3 -- with the halt
        # threshold set below the realized drawdown, it must never get a
        # trade; with it set above, it must.
        entry_day, hard_stop_day = "2026-01-05", "2026-01-06"
        chains = {
            ("F", EXP1): make_chain(EXP1, spot=100.0),
            ("GME", EXP1): make_chain(EXP1, spot=100.0),
        }
        days = trading_days(entry_day, "2026-03-01")
        spots = {(sym, d): 100.0 for sym in ("F", "GME") for d in days}
        history = FakeMultiHistory(
            chains, value_factors={"F": {d: 5.0 for d in days if d >= hard_stop_day}})

        f_cfg = CreditVariantConfig(
            name="test_s2_hs", short_put_delta=0.25, min_dte=30, max_dte=45,
            target_dte=39, min_credit_frac=0.0, exit_on_breach=False,
            hard_stop_credit_mult=1.5)
        gme_cfg = CreditVariantConfig(
            name="test_s3", short_put_delta=0.16, short_call_delta=0.16,
            min_dte=10, max_dte=25, target_dte=18, min_credit_frac=0.0,
            exit_on_breach=False)
        f_sleeve = Sleeve(sleeve_id="S2", tickers=["F"], is_satellite=False,
                          kind="credit", credit_cfg=f_cfg)
        gme_sleeve = Sleeve(sleeve_id="S3", tickers=["GME"], is_satellite=True,
                            kind="credit", credit_cfg=gme_cfg)

        blocked = PortfolioBacktestEngine(
            history, spots, PortfolioConfig(drawdown_halt_pct=0.001,
                                            starting_equity=10_000.0)
        ).run([f_sleeve, gme_sleeve], entry_day, days[-1])
        assert "F" in {t.underlying for t in blocked.trades}   # the stop-out happened
        assert "GME" not in {t.underlying for t in blocked.trades}   # halted

        allowed = PortfolioBacktestEngine(
            history, spots, PortfolioConfig(drawdown_halt_pct=0.99,
                                            starting_equity=10_000.0)
        ).run([f_sleeve, gme_sleeve], entry_day, days[-1])
        assert "GME" in {t.underlying for t in allowed.trades}


class TestWheelCycle:
    def test_rides_through_a_mid_life_dip_to_assignment(self):
        # Regression for the exit_on_breach bug: spot dips BELOW the short
        # put strike (~94) midway through the position's life, then
        # recovers, then lands between wing (~92) and short strike at
        # actual settlement. A breach exit would have closed this in cash
        # the moment spot first dipped -- it must instead ride through to
        # assignment.
        entry_day = "2026-01-05"
        expiry_day = EXP1
        dip_day = "2026-01-20"   # well before expiry, spot dips through 94
        chains = {("F", EXP1): make_chain(EXP1, spot=100.0)}
        history = FakeMultiHistory(chains)
        all_days = trading_days(entry_day, expiry_day)
        spots = {("F", d): 100.0 for d in all_days}
        spots[("F", dip_day)] = 85.0     # deep below the short strike, mid-life
        spots[("F", expiry_day)] = 93.0  # settles between wing and short strike

        wcfg = WheelConfig(name="test_wheel_dip", put_delta_target=0.25,
                           put_wing_width_frac=0.02, put_min_dte=30,
                           put_max_dte=45, put_min_credit_frac=0.0,
                           hard_stop_credit_mult=None)  # isolate breach only
        sleeve = Sleeve(sleeve_id="S1", tickers=["F"], is_satellite=False,
                        kind="wheel", wheel_cfg=wcfg)
        result = PortfolioBacktestEngine(
            history, spots, PortfolioConfig(starting_equity=10_000.0)
        ).run([sleeve], entry_day, expiry_day)

        assert [t.exit_reason for t in result.trades] == ["assigned"]

    def test_assignment_then_covered_call_then_called_away(self):
        # Phase A entry on day 1, spot=100: short put ~94, wing ~92
        # (wing_width_frac=0.02 * spot=100 -> 2.0, snapped to nearest
        # integer strike). Settlement spot=93 lands strictly between the
        # wing and the short strike -> assignment.
        entry_day = "2026-01-05"
        expiry_day = EXP1  # 2026-02-13, a Friday
        chains = {("F", EXP1): make_chain(EXP1, spot=100.0),
                 ("F", EXP2): make_chain(EXP2, spot=93.0)}
        history = FakeMultiHistory(chains)
        all_days = trading_days(entry_day, "2026-04-01")
        spots = {("F", d): 100.0 for d in all_days}
        spots[("F", expiry_day)] = 93.0   # assignment day
        # After assignment, Phase B enters at the next weekly checkpoint;
        # keep spot flat at 93 so the call decays toward expiring worthless
        # until the final settlement, then jump above the call strike.
        for d in all_days:
            if d > expiry_day:
                spots[("F", d)] = 93.0
        spots[("F", EXP2)] = 150.0   # called away well above any call strike

        wcfg = WheelConfig(name="test_wheel", put_delta_target=0.25,
                           put_wing_width_frac=0.02, put_min_dte=30,
                           put_max_dte=45, put_min_credit_frac=0.0,
                           call_delta_target=0.25)
        sleeve = Sleeve(sleeve_id="S1", tickers=["F"], is_satellite=False,
                        kind="wheel", wheel_cfg=wcfg)
        cfg = PortfolioConfig(starting_equity=10_000.0)
        engine = PortfolioBacktestEngine(history, spots, cfg)
        result = engine.run([sleeve], entry_day, "2026-04-01")

        reasons = [t.exit_reason for t in result.trades]
        assert "assigned" in reasons
        assert "called_away" in reasons
        kinds = [t.kind for t in result.trades]
        assert "covered_call" in kinds
        # Assignment itself is cash-neutral (economics move into cost
        # basis); the called-away trade should show a positive P&L since
        # settlement (150) is far above any plausible call strike/basis.
        called_away = next(t for t in result.trades if t.exit_reason == "called_away")
        assert called_away.pnl > 0
