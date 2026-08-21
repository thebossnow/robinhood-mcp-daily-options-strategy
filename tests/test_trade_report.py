"""The reporting contract: every field the framework asks for, and the
refusal to claim confirmation that did not happen."""

import pandas as pd
import pytest

from options_trader.reporting.trade_report import (
    ConfirmationLine, _measured_delta, _measured_iv,
)
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

    def _no_delta_column(self, **over):
        # Neither production provider publishes a delta column, so this is
        # the LIVE case, not an edge case: build_position falls back to a
        # Black-Scholes delta off the leg's IV.
        chain = _chain().drop(columns=["delta"])
        for k, v in over.items():
            chain[k] = v
        cfg = CreditVariantConfig(name="t", short_put_delta=0.22,
                                  short_call_delta=None,
                                  wing_width_frac=0.02, min_credit_frac=0.0)
        pos = build_position(chain, SPOT, "SPY", "2026-08-24", "2026-10-02",
                             39, cfg)
        if pos is None:
            return None
        sizing = size_position(25_000.0, spread_capital_at_risk(
            max(pos.widths().values()), pos.credit))
        return report_credit_position(pos, sizing, 25_000.0,
                                      "put_credit_spread")

    def test_model_delta_confirms_but_is_labelled_as_model(self):
        rep = self._no_delta_column()
        assert rep.confirmation.confirmed
        assert rep.confirmation.delta_source == "model"
        assert "model-derived" in rep.render()
        assert "NOT CONFIRMED" not in rep.render()

    def test_chain_delta_is_labelled_as_chain(self):
        _, _, rep = self._report()
        assert rep.confirmation.delta_source == "chain"
        assert "chain-quoted" in rep.render()

    def test_the_model_delta_is_signed_like_a_put(self):
        rep = self._no_delta_column()
        assert rep.confirmation.short_delta < 0

    def test_a_chain_with_no_iv_cannot_be_confirmed_at_all(self):
        # No delta column AND no IV: no strike is selectable, because the
        # model has nothing to compute a delta from either.
        assert self._no_delta_column(iv=0.0) is None

    def test_delta_source_reaches_the_dict(self):
        assert self._no_delta_column().to_dict()["delta_source"] == "model"


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


class TestMeasuredVersusMissing:
    """The confirmation gate distinguishes "measured" from "absent". It
    used to use truthiness, which got both ends wrong: 0.0 (a measurement)
    read as absent, and NaN (absent) is truthy in Python so it read as a
    measurement and rendered `at nan delta` above the word "confirmed"."""

    def test_a_zero_delta_is_a_measurement(self):
        assert _measured_delta(0.0) == 0.0

    def test_a_nan_delta_is_not(self):
        assert _measured_delta(float("nan")) is None

    def test_a_missing_delta_is_not(self):
        assert _measured_delta(None) is None

    def test_an_ordinary_delta_survives(self):
        assert _measured_delta(-0.15) == -0.15

    def test_a_zero_iv_is_the_repos_missing_sentinel(self):
        """Every provider coerces an absent IV to 0.0, so unlike delta,
        zero here means the field was not published."""
        assert _measured_iv(0.0) is None

    def test_a_nan_or_negative_iv_is_missing(self):
        assert _measured_iv(float("nan")) is None
        assert _measured_iv(-0.1) is None

    def test_an_ordinary_iv_survives(self):
        assert _measured_iv(0.18) == 0.18

    def test_a_nan_delta_can_never_render_as_confirmed(self):
        line = ConfirmationLine(short_strike=700.0,
                                short_delta=_measured_delta(float("nan")),
                                short_iv=_measured_iv(0.20),
                                delta_source="chain")
        assert not line.confirmed
        assert "NOT CONFIRMED" in line.render()
        assert "nan" not in line.render()

    def test_a_zero_delta_with_a_real_iv_confirms(self):
        line = ConfirmationLine(short_strike=700.0,
                                short_delta=_measured_delta(0.0),
                                short_iv=_measured_iv(0.20),
                                delta_source="model")
        assert line.confirmed
        assert "0.00 delta" in line.render()

    def test_a_leg_with_neither_still_fails(self):
        line = ConfirmationLine(short_strike=700.0,
                                short_delta=_measured_delta(None),
                                short_iv=_measured_iv(0.0),
                                delta_source="none")
        assert not line.confirmed
        assert "no delta and no IV" in line.render()
