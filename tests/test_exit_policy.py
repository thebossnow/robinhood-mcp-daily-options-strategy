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
        msg, st = self._run(tmp_path, _cfg(stop_loss_mode="position_max_loss"))
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
