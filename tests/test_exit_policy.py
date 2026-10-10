"""Income-framework exit policy: 20% target, account-equity loss stop."""
from datetime import date

import pytest

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.risk.exit_policy import stop_hit, stop_threshold
from options_trader.signals.butterfly import IRON_FLY
from options_trader.signals.credit import CreditVariantConfig
from tests.test_income_scripts import _cfg, _chain, _condor, _open


class TestThreshold:
    def test_income_config_defaults_to_account_equity_5pct(self):
        cfg = StrategyConfig.from_json("configs/income_framework.json")
        assert cfg.stop_loss_mode == "account_equity"
        assert cfg.max_loss_pct_account == 0.05
        assert stop_threshold(cfg, 999.0) == pytest.approx(3750.0)

    def test_position_mode(self):
        assert StrategyConfig().max_loss_pct_position == 0.50
        cfg = _cfg(stop_loss_mode="position_max_loss", max_loss_pct_position=0.05)
        assert stop_threshold(cfg, 2000.0) == pytest.approx(100.0)

    def test_credit_book_has_no_stop(self):
        assert stop_threshold(StrategyConfig(), 1000.0) is None
        assert stop_threshold(
            StrategyConfig.from_json("configs/credit_paper.json"), 1000.0) is None

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            stop_threshold(_cfg(stop_loss_mode="nope"), 1.0)

    def test_loss_scales_with_contracts(self):
        cfg = _cfg(stop_loss_mode="account_equity", account_equity=10_000.0)
        assert not stop_hit(cfg, 2.0, 2, 0)[0]      # $400 < $500
        assert stop_hit(cfg, 2.5, 2, 0)[0]          # $500 >= $500
        assert not stop_hit(cfg, -3.0, 5, 0)[0]     # a profit never stops


class TestManagement:
    class _Losing:
        def get_chain(self, underlying, expiration):
            c = _chain()
            c.loc[0, ["bid", "ask"]] = [9.4, 9.5]
            return type("Snap", (), {"chain": c})()

    class _Unmarkable:
        def get_chain(self, underlying, expiration):
            c = _chain()
            c.loc[0, ["bid", "ask"]] = [0.0, 0.0]
            return type("Snap", (), {"chain": c})()

    DAY = date(2026, 9, 1)
    VCFG = CreditVariantConfig(name="income_condor15", profit_take_frac=0.20)

    def _run(self, tmp_path, cfg, provider=None, day=None):
        from scripts.manage_credit import manage_position
        j, rec = _open(tmp_path, _condor())
        msg = manage_position(rec, cfg, j, PaperBroker(cfg, j),
                              provider or self._Losing(), None,
                              day or self.DAY, vcfg=self.VCFG)
        return msg, j.get(rec.id).status

    def test_account_stop_fires(self, tmp_path):
        # ~$610 loss on 1 contract vs 5% of $10k = $500
        msg, st = self._run(tmp_path, _cfg(stop_loss_mode="account_equity",
                                           account_equity=10_000.0))
        assert "STOP LOSS" in msg and st == "closed"

    def test_account_stop_holds_below_threshold(self, tmp_path):
        # same loss vs 5% of $50k = $2,500
        msg, st = self._run(tmp_path, _cfg(stop_loss_mode="account_equity"))
        assert "hold" in msg and st == "open"

    def test_position_mode_fires(self, tmp_path):
        msg, st = self._run(tmp_path, _cfg(stop_loss_mode="position_max_loss",
                                           max_loss_pct_position=0.2))
        assert "STOP LOSS" in msg and st == "closed"

    def test_no_mode_no_stop(self, tmp_path):
        msg, st = self._run(tmp_path, _cfg())
        assert "hold" in msg and st == "open"

    def test_time_exit_due_but_unmarkable_is_flagged(self, tmp_path):
        msg, st = self._run(tmp_path, _cfg(), self._Unmarkable(),
                            date(2026, 9, 25))
        assert "CLOSE MANUALLY" in msg and st == "open"


class TestBacktestNeverHoldsOnPolicy:
    def test_every_income_variant_time_exits_before_expiry(self):
        from options_trader.signals.income import PROFILES
        vs = [v for p in PROFILES.values() for v in p.variants.values()]
        for v in vs + [IRON_FLY]:
            assert v.time_exit_threshold(v.min_dte) >= 1, v.name


class TestSingleProfitTarget:
    def test_config_and_variants_agree_at_20pct(self):
        from options_trader.signals.income import PROFILES
        cfg = StrategyConfig.from_json("configs/income_framework.json")
        assert cfg.take_profit_pct == 0.2
        for p in PROFILES.values():
            for v in p.variants.values():
                assert v.profit_take_frac == cfg.take_profit_pct
        assert IRON_FLY.profit_take_frac == cfg.take_profit_pct

    def test_management_applies_the_config_value(self):
        from scripts.manage_income import income_exit_config
        v = income_exit_config(CreditVariantConfig(name="x"), _cfg(take_profit_pct=0.2))
        assert v.profit_take_frac == 0.2


class TestStopLossFracFallback:
    def test_variant_stop_used_when_no_policy(self, tmp_path):
        from scripts.manage_credit import manage_position
        j, rec = _open(tmp_path, _condor())
        v = CreditVariantConfig(name=rec.kind, stop_loss_frac=1.0)
        msg = manage_position(rec, _cfg(), j, PaperBroker(_cfg(), j),
                              TestManagement._Losing(), None,
                              date(2026, 9, 1), vcfg=v)
        assert "stop_loss_frac" in j.get(rec.id).notes or "STOP LOSS" in msg
        assert j.get(rec.id).status == "closed"


class TestBacktestStopFallback:
    """stop_loss_frac must apply whenever no stop_loss_mode is set, even
    when an exit_policy object IS passed (review bug, 2026-10-10)."""

    def _exit(self, policy, frac):
        import pandas as pd
        from dataclasses import replace
        from options_trader.backtest.managed import ManagedBacktestEngine
        eng = ManagedBacktestEngine(None, {}, exit_policy=policy)
        v = replace(CreditVariantConfig(name="income_condor15"),
                    profit_take_frac=0.2, exit_on_breach=False,
                    stop_loss_frac=frac)
        return eng._check_exit(_condor(), v, "2026-08-25", 600.0,
                               pd.DataFrame(), ["2026-08-25"])

    def test_fallback_fires_with_policy_but_no_mode(self):
        t = self._exit(StrategyConfig(), 1.0)
        assert t is not None and t.exit_reason == "stop_loss"

    def test_fallback_fires_with_no_policy(self):
        t = self._exit(None, 1.0)
        assert t is not None and t.exit_reason == "stop_loss"

    def test_no_stop_anywhere_holds(self):
        assert self._exit(StrategyConfig(), None) is None
