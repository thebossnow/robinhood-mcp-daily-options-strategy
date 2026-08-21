"""The income entry/management scripts: variant resolution across
profiles, IV-spike grading from a live chain, and the explicit-vcfg
override that keeps a 7-DTE weekly from inheriting a 21-DTE time exit."""

from datetime import date, timedelta

import pandas as pd
import pytest

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.journal import Journal
from options_trader.risk.iv_spike import IVSpikeConfig
from options_trader.signals.credit import CreditLeg, CreditPosition
from options_trader.signals.income import EVIDENCE_ADJUSTED, get_profile


def _cfg(**over):
    base = dict(account_equity=50_000.0, max_risk_per_trade_pct=0.05,
                daily_loss_limit_pct=0.12, max_open_positions=6,
                max_consecutive_losses=5)
    base.update(over)
    return StrategyConfig(**base)


def _condor(variant="income_condor15", entry_iv=0.18):
    legs = [
        CreditLeg("put", 650.0, -1, 3.4, 3.5, -0.15, entry_iv),
        CreditLeg("put", 620.0, 1, 1.4, 1.5, -0.08, entry_iv + 0.02),
        CreditLeg("call", 720.0, -1, 2.4, 2.5, 0.15, 0.16),
        CreditLeg("call", 750.0, 1, 1.0, 1.1, 0.07, 0.15),
    ]
    pos = CreditPosition(
        underlying="SPY", variant=variant, entry_date="2026-08-24",
        expiration="2026-10-02", dte_at_entry=39, spot_at_entry=690.0,
        legs=legs, credit_mid=3.4, credit=3.3, credit_frac=0.11)
    pos.max_loss = 30.0 - 3.3
    return pos


def _open(tmp_path, pos):
    j = Journal(tmp_path / "j.db")
    tid = j.record_credit_entry(pos.to_dict(), 1)
    return j, j.get(tid)


def _chain(put_iv=0.18, put_delta=-0.15):
    return pd.DataFrame([
        {"type": "put", "strike": 650.0, "bid": 3.4, "ask": 3.5,
         "iv": put_iv, "delta": put_delta},
        {"type": "put", "strike": 620.0, "bid": 1.4, "ask": 1.5,
         "iv": 0.20, "delta": -0.08},
        {"type": "call", "strike": 720.0, "bid": 2.4, "ask": 2.5,
         "iv": 0.16, "delta": 0.15},
        {"type": "call", "strike": 750.0, "bid": 1.0, "ask": 1.1,
         "iv": 0.15, "delta": 0.07},
    ])


class TestVariantResolution:
    def test_resolves_a_variant_from_the_active_profile(self):
        from scripts.manage_income import variant_config
        v = variant_config("income_condor15", EVIDENCE_ADJUSTED)
        assert v.wing_width_frac == 0.04

    def test_resolves_a_variant_from_another_profile(self):
        """A position entered under as_specified must keep being managed by
        its own rules after the operator switches the default profile."""
        from scripts.manage_income import variant_config
        v = variant_config("spec_weekly_put_25d", EVIDENCE_ADJUSTED)
        assert v.time_exit_dte == 1

    def test_unknown_variant_falls_back_to_defaults(self):
        from scripts.manage_income import variant_config
        assert variant_config("renamed_later", EVIDENCE_ADJUSTED).name == \
            "renamed_later"


class TestExplicitVariantOverride:
    """The regression the vcfg parameter exists to prevent.

    spec_weekly_put_25d enters at ~4 DTE and time-exits at
    min(time_exit_dte=1, 4 * 0.5 = 2) = 1 DTE. CreditVariantConfig's
    defaults give min(21, 2) = 2. With 2 days left those disagree: the
    defaults close the position, the variant's own config holds it. Before
    manage_position took a vcfg, a variant outside VALIDATED silently got
    the defaults, so this position closed a day early every time.
    """

    def _weekly_open(self, tmp_path):
        pos = _condor(variant="spec_weekly_put_25d")
        pos.expiration = "2026-08-27"
        pos.dte_at_entry = 4
        return _open(tmp_path, pos)

    class _Marking:
        """Marks the legs at roughly entry value, so the profit target
        cannot fire and the time exit is what decides."""
        def get_chain(self, underlying, expiration):
            return type("Snap", (), {"chain": _chain()})()

    TWO_DTE_LEFT = date(2026, 8, 25)

    def test_without_an_override_the_defaults_close_it_early(self, tmp_path):
        from scripts.manage_credit import manage_position
        j, rec = self._weekly_open(tmp_path)
        msg = manage_position(rec, _cfg(), j, PaperBroker(_cfg(), j),
                              self._Marking(), None, self.TWO_DTE_LEFT)
        assert "TIME EXIT at 2 DTE" in msg
        assert j.get(rec.id).status == "closed"

    def test_with_the_override_the_variants_own_rule_holds_it(self, tmp_path):
        from scripts.manage_credit import manage_position
        from scripts.manage_income import variant_config
        j, rec = self._weekly_open(tmp_path)
        vcfg = variant_config("spec_weekly_put_25d", EVIDENCE_ADJUSTED)
        assert vcfg.time_exit_threshold(4) == 1
        msg = manage_position(rec, _cfg(), j, PaperBroker(_cfg(), j),
                              self._Marking(), None, self.TWO_DTE_LEFT,
                              vcfg=vcfg)
        assert "hold" in msg
        assert j.get(rec.id).status == "open"

    def test_the_override_still_closes_once_its_own_threshold_arrives(self, tmp_path):
        from scripts.manage_credit import manage_position
        from scripts.manage_income import variant_config
        j, rec = self._weekly_open(tmp_path)
        vcfg = variant_config("spec_weekly_put_25d", EVIDENCE_ADJUSTED)
        msg = manage_position(rec, _cfg(), j, PaperBroker(_cfg(), j),
                              self._Marking(), None, date(2026, 8, 26),
                              vcfg=vcfg)
        assert "TIME EXIT at 1 DTE" in msg


class TestShortLegQuote:
    def test_reads_the_put_side_first(self):
        from scripts.manage_income import short_leg_quote
        legs = [{"type": "put", "strike": 650.0, "side": -1},
                {"type": "call", "strike": 720.0, "side": -1}]
        iv, delta = short_leg_quote(legs, _chain(put_iv=0.40, put_delta=-0.45))
        assert iv == pytest.approx(0.40) and delta == pytest.approx(0.45)

    def test_falls_back_to_the_call_side(self):
        from scripts.manage_income import short_leg_quote
        legs = [{"type": "call", "strike": 720.0, "side": -1}]
        iv, delta = short_leg_quote(legs, _chain())
        assert iv == pytest.approx(0.16)

    def test_missing_strike_yields_nothing(self):
        from scripts.manage_income import short_leg_quote
        legs = [{"type": "put", "strike": 999.0, "side": -1}]
        assert short_leg_quote(legs, _chain()) == (None, None)

    def test_zero_iv_and_delta_read_as_absent(self):
        from scripts.manage_income import short_leg_quote
        legs = [{"type": "put", "strike": 650.0, "side": -1}]
        assert short_leg_quote(legs, _chain(put_iv=0.0, put_delta=0.0)) == \
            (None, None)


class TestGrading:
    def test_quiet_position_produces_no_line(self, tmp_path):
        from scripts.manage_income import grade_position
        j, rec = _open(tmp_path, _condor())
        assert grade_position(rec, j, _chain(), IVSpikeConfig()) is None

    def test_spike_with_a_safe_strike_watches(self, tmp_path):
        from scripts.manage_income import grade_position
        j, rec = _open(tmp_path, _condor(entry_iv=0.15))
        line = grade_position(rec, j, _chain(put_iv=0.40, put_delta=-0.20),
                              IVSpikeConfig())
        assert "WATCH" in line

    def test_spike_with_a_threatened_strike_defends(self, tmp_path):
        from scripts.manage_income import grade_position
        j, rec = _open(tmp_path, _condor(entry_iv=0.15))
        line = grade_position(rec, j, _chain(put_iv=0.40, put_delta=-0.45),
                              IVSpikeConfig())
        assert "DEFEND" in line

    def test_no_entry_iv_cannot_be_graded(self, tmp_path):
        from scripts.manage_income import grade_position
        j, rec = _open(tmp_path, _condor(entry_iv=0.0))
        assert grade_position(rec, j, _chain(put_iv=0.40), IVSpikeConfig()) is None

    def test_unquoted_leg_cannot_be_graded(self, tmp_path):
        from scripts.manage_income import grade_position
        j, rec = _open(tmp_path, _condor())
        bare = _chain().drop(columns=["iv"]).assign(iv=0.0)
        assert grade_position(rec, j, bare, IVSpikeConfig()) is None


class TestEntryShortIV:
    def test_prefers_the_put_side(self):
        from scripts.manage_income import entry_short_iv
        entry = _condor(entry_iv=0.19).to_dict()
        assert entry_short_iv(entry) == pytest.approx(0.19)

    def test_missing_legs_yield_none(self):
        from scripts.manage_income import entry_short_iv
        assert entry_short_iv({}) is None


class TestScanHelpers:
    def test_pick_expiration_targets_the_middle_of_the_window(self):
        from scripts.scan_income import pick_expiration
        today = date(2026, 8, 24)
        exps = ["2026-08-28", "2026-09-25", "2026-10-02", "2026-12-18"]
        assert pick_expiration(exps, today, 30, 45, 40) == "2026-10-02"

    def test_pick_expiration_returns_none_outside_the_window(self):
        from scripts.scan_income import pick_expiration
        assert pick_expiration(["2026-08-28"], date(2026, 8, 24),
                               30, 45, 40) is None

    def test_iv_history_round_trips(self, tmp_path):
        from scripts.scan_income import load_iv_history, save_iv_observation
        path = str(tmp_path / "iv.json")
        assert load_iv_history(path) == {}
        save_iv_observation(path, "2026-08-24", 0.1234567)
        save_iv_observation(path, "2026-08-25", 0.20)
        got = load_iv_history(path)
        assert got == {"2026-08-24": 0.123457, "2026-08-25": 0.2}

    def test_corrupt_iv_history_is_treated_as_empty(self, tmp_path):
        from scripts.scan_income import load_iv_history
        path = tmp_path / "iv.json"
        path.write_text("{not json")
        assert load_iv_history(str(path)) == {}

    def test_profile_lookup_is_shared_with_the_scanner(self):
        assert get_profile("as_specified").name == "as_specified"


class TestScanEndToEnd:
    """main() through every gate, against a synthetic chain.

    The chain's prices are arbitrary — nothing here is a claim about what
    SPY pays. What is asserted is the plumbing: that the gates fire in
    order, that refusals name their gate, and that a dry run reports the
    same contract counts a real run would.
    """

    SPOT = 762.60
    MONDAY = date(2026, 8, 24)

    def _chain(self):
        rows = []
        for i in range(1, 60):
            k = round(self.SPOT * (1 - 0.002 * i), 0)
            px = max(0.05, 22.0 * (0.90 ** i))
            rows.append({"type": "put", "strike": k, "bid": round(px - 0.05, 2),
                         "ask": round(px + 0.05, 2), "iv": 0.17 + 0.001 * i,
                         "delta": -round(max(0.01, 0.48 * (0.90 ** i)), 4),
                         "open_interest": 4000, "volume": 500})
        for i in range(1, 60):
            k = round(self.SPOT * (1 + 0.002 * i), 0)
            px = max(0.05, 20.0 * (0.90 ** i))
            rows.append({"type": "call", "strike": k, "bid": round(px - 0.05, 2),
                         "ask": round(px + 0.05, 2), "iv": 0.14 + 0.0005 * i,
                         "delta": round(max(0.01, 0.46 * (0.90 ** i)), 4),
                         "open_interest": 4000, "volume": 500})
        return pd.DataFrame(rows)

    def _run(self, tmp_path, capsys, today=None, profile="evidence_adjusted",
             vix=None, extra=()):
        import sys
        from unittest import mock
        import scripts.scan_income as si

        today = today or self.MONDAY
        chain = self._chain()

        class FakeProvider:
            def get_expirations(self, u):
                return [(today + timedelta(days=d)).isoformat()
                        for d in (4, 7, 11, 32, 39, 46)]

            def get_chain(self, u, exp):
                dte = (date.fromisoformat(exp) - today).days
                return type("Snap", (), {"chain": chain, "spot": 762.60,
                                         "dte": dte, "expiration": exp})()

        class FakeDate(date):
            @classmethod
            def today(cls):
                return today

        argv = ["scan_income.py", "--profile", profile, "--dry-run",
                "--events", "configs/events_EXAMPLE.json",
                "--journal", str(tmp_path / "j.db"),
                "--iv-history", str(tmp_path / "iv.json"), *extra]
        series = pd.Series(vix if vix is not None
                           else [15.0, 15.2, 15.1, 15.4, 15.3, 15.6])
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(si, "date", FakeDate), \
             mock.patch.object(si, "fetch_vix_closes", lambda n: series), \
             mock.patch("options_trader.data.provider.YFinanceProvider",
                        FakeProvider):
            rc = si.main()
        return rc, capsys.readouterr().out

    def test_a_monday_produces_reports_for_every_variant(self, tmp_path, capsys):
        rc, out = self._run(tmp_path, capsys)
        assert rc == 0
        for name in EVIDENCE_ADJUSTED.variants:
            assert name in out, name
        assert "risk percentage" in out

    def test_wednesday_stops_at_the_cadence_gate(self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys, today=date(2026, 8, 26))
        assert "NO QUALIFYING TRADE" in out and "not an entry day" in out
        assert "vol regime" not in out          # never got that far

    def test_force_overrides_the_cadence_gate(self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys, today=date(2026, 8, 26),
                           extra=["--force"])
        assert "(forced)" in out and "vol regime" in out

    def test_a_vix_spike_stops_before_any_chain_is_fetched(self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys,
                           vix=[15.0, 16.0, 18.0, 22.0, 26.0, 29.0])
        assert "vol regime" in out and "NO QUALIFYING TRADE" in out
        assert "risk percentage" not in out

    def test_vix_above_the_ceiling_stops(self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys, vix=[31.0] * 6)
        assert "entry ceiling" in out

    def test_missing_vix_fails_closed(self, tmp_path, capsys):
        import sys
        from unittest import mock
        import scripts.scan_income as si

        class FakeDate(date):
            @classmethod
            def today(cls):
                return self.MONDAY

        argv = ["scan_income.py", "--dry-run", "--journal",
                str(tmp_path / "j.db"), "--iv-history", str(tmp_path / "iv.json")]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(si, "date", FakeDate), \
             mock.patch.object(si, "fetch_vix_closes", lambda n: None):
            si.main()
        out = capsys.readouterr().out
        assert "refusing new entries defensively" in out

    def test_the_spanned_event_list_reaches_the_report(self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys)
        assert "event(s) before expiry" in out
        assert "FOMC" in out and "NFP" in out

    def test_index_csps_are_reported_as_disabled_not_silently_skipped(
            self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys)
        assert "cash-secured put: disabled" in out

    def test_as_specified_refuses_its_cash_secured_put_on_size(
            self, tmp_path, capsys):
        _, out = self._run(tmp_path, capsys, profile="as_specified")
        assert "spec_csp_25d: NO QUALIFYING TRADE" in out
        assert "sizing" in out

    def test_dry_run_consumes_heat_across_variants(self, tmp_path, capsys):
        """Later variants must be sized against the earlier ones, or the
        preview overstates the book."""
        _, out = self._run(tmp_path, capsys)
        risks = [float(line.split("$")[1].replace(",", ""))
                 for line in out.splitlines() if "capital at risk" in line]
        assert risks
        cap = 0.20 * 75_000.0
        assert sum(risks) <= cap
