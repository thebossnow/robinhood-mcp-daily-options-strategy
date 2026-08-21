"""The digest is an ACCOUNT view, so it must cover every credit-like book.

scan_income.py opens real paper capital under the 'income' tag. A digest
that read only 'credit' — as this one did — understated open exposure by
whatever the income book happened to be carrying, on a cron job whose whole
job is telling the operator what is at risk.
"""

import importlib.util
from pathlib import Path

import pytest

from options_trader.journal import Journal
from options_trader.journal.journal import CREDIT_STRATEGIES

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "daily_digest.py"
_spec = importlib.util.spec_from_file_location("daily_digest", _PATH)
daily_digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_digest)


def _credit_entry(journal, strategy, credit=3.00, width=30.0, contracts=1,
                  expiration="2099-01-15"):
    legs = [{"type": "put", "strike": 650.0, "side": -1},
            {"type": "put", "strike": 650.0 - width, "side": 1}]
    return journal.record_credit_entry(
        {"underlying": "SPY", "expiration": expiration,
         "variant": f"{strategy}_variant", "legs": legs,
         "credit": credit, "max_width": width},
        contracts, notes=f"{strategy} entry", strategy=strategy,
    )


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


class TestDigestCoversEveryBook:
    def test_open_positions_include_the_income_book(self, journal, capsys,
                                                    monkeypatch):
        _credit_entry(journal, "credit")
        _credit_entry(journal, "income")
        journal.close()
        monkeypatch.setattr("sys.argv",
                            ["daily_digest.py", "--journal",
                             str(journal.path)])
        daily_digest.main()
        out = capsys.readouterr().out
        assert "OPEN POSITIONS (2)" in out
        assert "[credit]" in out and "[income]" in out

    def test_total_max_loss_sums_both_books(self, journal, capsys,
                                            monkeypatch):
        # (30 - 3) * 100 = $2,700 per book, two books.
        _credit_entry(journal, "credit")
        _credit_entry(journal, "income")
        journal.close()
        monkeypatch.setattr("sys.argv",
                            ["daily_digest.py", "--journal",
                             str(journal.path)])
        daily_digest.main()
        assert "$5,400" in capsys.readouterr().out

    def test_every_credit_book_gets_its_own_stats_section(self, journal,
                                                          capsys, monkeypatch):
        journal.close()
        monkeypatch.setattr("sys.argv",
                            ["daily_digest.py", "--journal",
                             str(journal.path)])
        daily_digest.main()
        out = capsys.readouterr().out
        for book in CREDIT_STRATEGIES:
            assert f"[{book}]" in out

    def test_closed_stats_are_not_pooled_across_books(self, journal, capsys,
                                                      monkeypatch):
        """Different variant registries and, off-index, different geometry.
        One blended win-rate would describe no strategy actually traded."""
        win = _credit_entry(journal, "credit")
        journal.record_exit(win, exit_value=1.00)        # +$200
        loss = _credit_entry(journal, "income")
        journal.record_exit(loss, exit_value=5.00)       # -$200
        journal.close()
        monkeypatch.setattr("sys.argv",
                            ["daily_digest.py", "--journal",
                             str(journal.path)])
        daily_digest.main()
        out = capsys.readouterr().out
        credit_sec = out.split("[credit]")[1].split("[income]")[0]
        income_sec = out.split("[income]")[1]
        assert "1 closed" in credit_sec and "1 closed" in income_sec
        # The sign survives per book rather than cancelling to zero.
        assert "200.0" in credit_sec and "-200.0" in income_sec

    def test_an_empty_journal_says_so_without_crashing(self, journal, capsys,
                                                       monkeypatch):
        journal.close()
        monkeypatch.setattr("sys.argv",
                            ["daily_digest.py", "--journal",
                             str(journal.path)])
        assert daily_digest.main() == 0
        out = capsys.readouterr().out
        assert "OPEN POSITIONS (0)" in out
        assert "no closed trades in any book yet" in out
