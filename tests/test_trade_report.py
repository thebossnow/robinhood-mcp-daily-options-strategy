"""The reporting contract: every field the framework asks for, and the
refusal to claim confirmation that did not happen."""

import pandas as pd
import pytest

from options_trader.reporting import (
    ConfirmationLine, report_credit_position, report_csp_position,
)
from options_trader.risk.sizing import (
    csp_capital_at_risk, size_position, spread_capital_at_risk,
)
from options_trader.signals.credit import CreditVariantConfig, build_position
from options_trader.signals.csp import CSPConfig, build_csp

SPOT = 762.60


def _chain():
    rows = []
    for strike, bid, ask, iv, delta in [
        (740.0, 7.90, 8.10, 0.17, -0.30),
        (725.0, 5.90, 6.10, 0.18, -0.22),
        (710.0, 4.40, 4.60, 0.19, -0.15),
        (695.0, 3.15, 3.35, 0.20, -0.10),
    ]:
        rows.append({"type": "put", "strike": strike, "bid": bid, "ask": ask,
                     "iv": iv, "delta": delta, "open_interest": 5000})
    for strike, bid, ask, iv, delta in [
        (790.0, 5.40, 5.60, 0.13, 0.22),
        (820.0, 2.40, 2.60, 0.12, 0.10),
    ]:
        rows.append({"type": "call", "strike": strike, "bid": bid, "ask": ask,
                     "iv": iv, "delta": delta, "open_interest": 5000})
    return pd.DataFrame(rows)


def _spread(**kw):
    cfg = CreditVariantConfig(name="t", short_put_delta=0.22,
                              short_call_delta=None, wing_width_frac=0.02,
                              min_credit_frac=0.0, **kw)
    return build_position(_chain(), SPOT, "SPY", "2026-08-24", "2026-10-02",
                          39, cfg)


class TestConfirmationLine:
    def test_confirmed_line_states_delta_and_iv(self):
        line = ConfirmationLine(725.0, -0.22, 0.18)
        assert line.confirmed
        assert "0.22 delta" in line.render() and "18.0%" in line.render()

    def test_missing_delta_is_not_a_confirmation(self):
        line = ConfirmationLine(725.0, None, 0.18)
        assert not line.confirmed
        assert "NOT CONFIRMED" in line.render() and "delta" in line.render()

    def test_missing_both_names_both(self):
        text = ConfirmationLine(725.0, None, None).render()
        assert "no delta and no IV" in text

    def test_iv_rank_note_is_appended_when_present(self):
        line = ConfirmationLine(725.0, -0.22, 0.18, "IV rank 41%")
        assert "IV rank 41%" in line.render()


class TestCreditReport:
    def _report(self, equity=25_000.0):
        pos = _spread()
        sizing = size_position(
            equity, spread_capital_at_risk(max(pos.widths().values()),
                                           pos.credit))
        return pos, sizing, report_credit_position(
            pos, sizing, equity, "put_credit_spread", "IV rank 41%",
            "1 event before expiry")

    def test_every_required_field_is_present(self):
        _, _, rep = self._report()
        text = rep.render()
        for required in ("entry date", "expiration", "strikes", "entry price",
                         "credit received", "risk percentage", "IV/delta check"):
            assert required in text, required

    def test_credit_received_is_total_dollars_not_per_share(self):
        pos, sizing, rep = self._report()
        assert rep.credit_received == pytest.approx(
            pos.credit * 100 * sizing.contracts)

    def test_risk_pct_matches_the_sizing_decision(self):
        _, sizing, rep = self._report()
        assert rep.risk_pct == sizing.risk_pct_of_equity
        assert rep.risk_pct <= rep.tier_pct

    def test_breakeven_is_short_put_less_credit(self):
        pos, _, rep = self._report()
        assert rep.breakeven == pytest.approx(pos.short_put_strike - pos.credit)

    def test_both_quoted_and_slipped_prices_are_shown(self):
        pos, _, rep = self._report()
        assert rep.credit_mid > rep.credit
        assert "quoted mid" in rep.render() and "after slippage" in rep.render()

    def test_to_dict_is_json_shaped(self):
        _, _, rep = self._report()
        d = rep.to_dict()
        assert d["iv_delta_confirmed"] is True
        assert d["credit_received_total"] == pytest.approx(rep.credit_received)

    def test_event_note_and_extra_notes_render(self):
        pos = _spread()
        sizing = size_position(25_000.0, spread_capital_at_risk(
            max(pos.widths().values()), pos.credit))
        rep = report_credit_position(pos, sizing, 25_000.0, "iron_condor",
                                     event_note="3 events",
                                     notes=["premium does not cover"])
        assert "3 events" in rep.render()
        assert "premium does not cover" in rep.render()

    def test_model_inferred_delta_is_not_a_confirmation(self):
        # No delta column: strikes are still selectable (build_position
        # falls back to Black-Scholes off the leg's IV), but the market
        # never quoted a delta, so the framework's "confirm the delta
        # before selling" step did not happen and the report says so.
        chain = _chain().drop(columns=["delta"])
        cfg = CreditVariantConfig(name="t", short_put_delta=0.22,
                                  short_call_delta=None,
                                  wing_width_frac=0.02, min_credit_frac=0.0)
        pos = build_position(chain, SPOT, "SPY", "2026-08-24", "2026-10-02",
                             39, cfg)
        sizing = size_position(25_000.0, spread_capital_at_risk(
            max(pos.widths().values()), pos.credit))
        rep = report_credit_position(pos, sizing, 25_000.0, "put_credit_spread")
        assert not rep.confirmation.confirmed
        assert "NOT CONFIRMED" in rep.render()


class TestCSPReport:
    def _report(self, equity=2_000_000.0):
        pos = build_csp(_chain(), SPOT, "SPY", "2026-08-24", "2026-10-02", 39,
                        CSPConfig(short_put_delta=0.22,
                                  min_annualized_return=0.0))
        sizing = size_position(equity,
                               csp_capital_at_risk(pos.strike, pos.credit))
        return pos, sizing, report_csp_position(pos, sizing, equity)

    def test_collateral_and_assignment_plan_are_in_the_notes(self):
        _, _, rep = self._report()
        text = rep.render()
        assert "cash-secured" in text and "collateral" in text
        assert "wheel" in text

    def test_structure_is_labelled_distinctly(self):
        _, _, rep = self._report()
        assert rep.structure == "cash_secured_put"

    def test_breakeven_is_strike_less_credit(self):
        pos, _, rep = self._report()
        assert rep.breakeven == pytest.approx(pos.strike - pos.credit)

    def test_small_account_report_shows_zero_contracts(self):
        _, sizing, rep = self._report(equity=25_000.0)
        assert sizing.contracts == 0
        assert rep.risk_pct == 0.0
