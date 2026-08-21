"""Event gates: derived payrolls, loaded calendars, entry blackout, the
unsatisfiability of "never hold through an event", and the premium test."""

from datetime import date

import pytest

from options_trader.risk.events import (
    EventCalendar, MarketEvent, check_events, nfp_dates, premium_compensates,
)


class TestDerivedNFP:
    def test_first_friday_of_each_month(self):
        got = nfp_dates(date(2026, 8, 1), date(2026, 10, 31))
        assert [e.day for e in got] == [
            date(2026, 8, 7), date(2026, 9, 4), date(2026, 10, 2)]

    def test_month_starting_on_a_friday_uses_the_first(self):
        # 2027-01-01 is a Friday.
        got = nfp_dates(date(2027, 1, 1), date(2027, 1, 31))
        assert got[0].day == date(2027, 1, 1)

    def test_respects_the_requested_window(self):
        # Window starts after August's first Friday, so August is excluded.
        got = nfp_dates(date(2026, 8, 10), date(2026, 9, 30))
        assert [e.day for e in got] == [date(2026, 9, 4)]

    def test_rolls_across_a_year_boundary(self):
        got = nfp_dates(date(2026, 12, 1), date(2027, 1, 31))
        assert len(got) == 2 and got[-1].day.year == 2027


class TestCalendarLoading:
    def test_missing_file_yields_an_empty_calendar_not_an_error(self):
        cal = EventCalendar.from_json("configs/does_not_exist.json")
        assert not cal.has_dates()

    def test_template_ships_with_no_dates(self):
        cal = EventCalendar.from_json("configs/events_TEMPLATE.json")
        assert not cal.has_dates()

    def test_example_file_loads(self):
        cal = EventCalendar.from_json("configs/events_EXAMPLE.json")
        assert cal.has_dates()

    def test_explicit_entry_overrides_the_derived_one(self):
        # BLS shifted this month's release; the explicit entry must win.
        shifted = MarketEvent(date(2026, 9, 8), "NFP", "holiday shift")
        cal = EventCalendar([shifted])
        days = [e.day for e in cal.between(date(2026, 9, 1), date(2026, 9, 30))]
        assert date(2026, 9, 8) in days
        assert date(2026, 9, 4) in days   # derived one still listed...
        explicit_note = [e.note for e in cal.between(date(2026, 9, 8),
                                                     date(2026, 9, 8))]
        assert explicit_note == ["holiday shift"]

    def test_derived_nfp_can_be_switched_off(self):
        cal = EventCalendar([], include_derived_nfp=False)
        assert cal.between(date(2026, 8, 1), date(2026, 12, 31)) == []


class TestEntryBlackout:
    def _cal(self):
        return EventCalendar([MarketEvent(date(2026, 9, 10), "CPI"),
                              MarketEvent(date(2026, 9, 16), "FOMC")])

    def test_entry_beside_an_event_is_refused(self):
        chk = check_events(date(2026, 9, 9), date(2026, 10, 16), self._cal())
        assert not chk.allowed and "CPI" in chk.reason

    def test_entry_on_the_event_day_is_refused(self):
        chk = check_events(date(2026, 9, 10), date(2026, 10, 16), self._cal())
        assert not chk.allowed

    def test_entry_well_clear_of_an_event_is_allowed(self):
        chk = check_events(date(2026, 9, 21), date(2026, 10, 16), self._cal())
        assert chk.allowed and chk.reason is None

    def test_widening_the_window_catches_more(self):
        far = check_events(date(2026, 9, 7), date(2026, 10, 16), self._cal())
        assert far.allowed
        wide = check_events(date(2026, 9, 7), date(2026, 10, 16), self._cal(),
                            blackout_days_before=5)
        assert not wide.allowed


class TestSpanIsUnavoidable:
    """The framework says never hold through a major event. At its own
    30-45 DTE window that refuses every trade — which is the finding."""

    def test_a_38_day_window_always_spans_events(self):
        cal = EventCalendar.from_json("configs/events_EXAMPLE.json")
        chk = check_events(date(2026, 8, 24), date(2026, 10, 1), cal)
        assert len(chk.spanned) >= 3

    def test_even_a_bare_calendar_spans_payrolls(self):
        chk = check_events(date(2026, 8, 24), date(2026, 10, 1),
                           EventCalendar([]))
        assert len(chk.spanned) >= 1

    def test_span_description_flags_an_unloaded_calendar(self):
        chk = check_events(date(2026, 8, 24), date(2026, 10, 1),
                           EventCalendar([]))
        assert "no event calendar loaded" in chk.describe_span()
        assert "NFP" in chk.describe_span()

    def test_loaded_calendar_reports_without_the_caveat(self):
        cal = EventCalendar.from_json("configs/events_EXAMPLE.json")
        chk = check_events(date(2026, 8, 24), date(2026, 10, 1), cal)
        assert "no event calendar loaded" not in chk.describe_span()

    def test_a_weekly_can_clear_every_event(self):
        cal = EventCalendar([MarketEvent(date(2026, 9, 10), "CPI")])
        chk = check_events(date(2026, 9, 14), date(2026, 9, 18), cal)
        assert chk.spanned == [] and chk.allowed


class TestPremiumCompensates:
    def test_no_events_passes_trivially(self):
        ok, why = premium_compensates(0.10, 762.6, 0)
        assert ok and "no spanned events" in why

    def test_typical_index_credit_fails_a_multi_event_budget(self):
        ok, why = premium_compensates(6.00, 762.60, 2)
        assert not ok and "does NOT cover" in why

    def test_a_large_credit_covers_it(self):
        ok, _ = premium_compensates(20.00, 762.60, 2)
        assert ok

    def test_required_multiple_tightens_the_test(self):
        assert premium_compensates(9.00, 762.60, 1)[0]
        assert not premium_compensates(9.00, 762.60, 1, required_multiple=2.0)[0]

    def test_move_estimate_is_a_tunable_not_a_constant(self):
        assert premium_compensates(6.00, 762.60, 1,
                                   typical_event_move_pct=0.005)[0]
