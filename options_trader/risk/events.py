"""Known-event risk: blackout windows and the "premium compensates" test.

The framework's rule is "never hold through major known events unless the
premium compensates". Taken literally at the framework's own 30-45 DTE
entry window, that rule refuses every trade: a 38-day window on a US index
always spans at least one CPI print and usually an FOMC decision plus a
payrolls report. There is no 30-45 DTE SPY position that holds through no
major event. A rule that can never be satisfied is not a risk control, it
is a rule that gets quietly ignored -- which is worse than not having it.

So the rule is split into the two distinct things it is actually reaching
for, both implementable:

  ENTRY BLACKOUT   Do not OPEN inside the days immediately around an event
                   (default: 1 day before through 1 day after). This is the
                   part that is both meaningful and satisfiable -- it stops
                   you from selling into a pre-event IV bid that collapses
                   the next morning, and from opening blind into a print.

  SPAN AWARENESS   Count the events a candidate position must live through
                   and report them. Spanning events is unavoidable at these
                   DTEs and is in fact where the variance risk premium is
                   earned; the response is to size and to know, not to
                   refuse. `premium_compensates()` is the "unless"
                   clause made checkable: the credit must cover the
                   underlying's historical move on days like these.

Event dates
-----------
FOMC decision dates are published by the Federal Reserve and CPI/PPI
release dates by the BLS; both are scheduled a year ahead but neither can
be derived from a rule. This module therefore ships NO hardcoded FOMC or
CPI calendar -- fabricated dates would be worse than none, because they
would silently blacklist the wrong days and clear the right ones. Load real
dates with `EventCalendar.from_json()` (see `configs/events_TEMPLATE.json`)
and refresh them each year from the primary sources.

Payrolls (NFP) is the one exception: it is defined as the first Friday of
the month, so `nfp_dates()` derives it exactly, with no calendar file.
Earnings for single names come from the MCP `get_earnings_calendar` call
the agent prompt already requires; this module takes them as input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# Event kinds treated as "major" by default. Anything else in a calendar
# file is still tracked but does not trigger a blackout unless named.
MAJOR_KINDS = ("FOMC", "CPI", "NFP", "EARNINGS")


@dataclass(frozen=True)
class MarketEvent:
    day: date
    kind: str
    note: str = ""

    def describe(self) -> str:
        tail = f" ({self.note})" if self.note else ""
        return f"{self.kind} on {self.day.isoformat()}{tail}"


def nfp_dates(start: date, end: date) -> list[MarketEvent]:
    """Non-farm payrolls: the first Friday of each month in [start, end].

    Derivable exactly, unlike FOMC/CPI. BLS occasionally shifts a release
    for a federal holiday, so a calendar file entry for a given month
    overrides this (see `EventCalendar.merge`).
    """
    out: list[MarketEvent] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        # weekday(): Monday=0 .. Friday=4
        first_friday = cursor + timedelta(days=(4 - cursor.weekday()) % 7)
        if start <= first_friday <= end:
            out.append(MarketEvent(first_friday, "NFP", "first Friday"))
        cursor = date(cursor.year + (cursor.month == 12),
                      1 if cursor.month == 12 else cursor.month + 1, 1)
    return out


@dataclass
class EventCalendar:
    events: list[MarketEvent] = field(default_factory=list)
    include_derived_nfp: bool = True

    @classmethod
    def from_json(cls, path: str | Path,
                  include_derived_nfp: bool = True) -> "EventCalendar":
        """Load {"events": [{"date": "YYYY-MM-DD", "kind": "FOMC",
        "note": "..."}]}. A missing file yields an EMPTY calendar rather
        than an error: the pipeline must run without one, reporting that
        event checks are unavailable, instead of refusing to start."""
        p = Path(path)
        if not p.exists():
            return cls([], include_derived_nfp)
        raw = json.loads(p.read_text())
        events = [
            MarketEvent(date.fromisoformat(e["date"]), e["kind"].upper(),
                        e.get("note", ""))
            for e in raw.get("events", [])
        ]
        return cls(events, include_derived_nfp)

    def merge(self, extra: list[MarketEvent]) -> list[MarketEvent]:
        """Explicit entries win over derived ones on the same (day, kind)."""
        explicit = {(e.day, e.kind) for e in self.events}
        return self.events + [e for e in extra
                              if (e.day, e.kind) not in explicit]

    def between(self, start: date, end: date,
                kinds: tuple[str, ...] = MAJOR_KINDS) -> list[MarketEvent]:
        """Events in [start, end], inclusive, sorted by date."""
        pool = list(self.events)
        if self.include_derived_nfp and "NFP" in kinds:
            pool = self.merge(nfp_dates(start, end))
        hits = [e for e in pool
                if start <= e.day <= end and e.kind in kinds]
        return sorted(hits, key=lambda e: (e.day, e.kind))

    def has_dates(self) -> bool:
        """False when no calendar file was loaded -- callers should report
        "event check unavailable" rather than "no events"."""
        return bool(self.events)


@dataclass
class EventCheck:
    allowed: bool
    blackout: list[MarketEvent] = field(default_factory=list)
    spanned: list[MarketEvent] = field(default_factory=list)
    calendar_loaded: bool = True

    @property
    def reason(self) -> str | None:
        if self.allowed:
            return None
        return "entry blackout: " + "; ".join(e.describe()
                                              for e in self.blackout)

    def describe_span(self) -> str:
        """What the position must live through. When no calendar file is
        loaded the derived NFP dates are still real, so they are reported
        as found -- with the gap named, because silence about FOMC and CPI
        would read as "there are none" rather than "we did not look"."""
        body = ("no major events before expiry" if not self.spanned else
                f"{len(self.spanned)} event(s) before expiry: "
                + ", ".join(e.describe() for e in self.spanned))
        if self.calendar_loaded:
            return body
        return (body + " — NOTE: no event calendar loaded, so FOMC/CPI "
                "dates were NOT checked (see configs/events_TEMPLATE.json)")


def check_events(entry_day: date, expiration: date, calendar: EventCalendar,
                 blackout_days_before: int = 1, blackout_days_after: int = 1,
                 kinds: tuple[str, ...] = MAJOR_KINDS) -> EventCheck:
    """Blackout the entry day itself, and report what the position spans.

    Fails OPEN when no calendar is loaded, and says so: an absent calendar
    file is an operator-configuration gap, not a market signal. The
    vol-regime gate fails closed because VIX is always fetchable and a
    failure there means something is broken; event dates are a file the
    operator maintains, and a fresh clone legitimately has none.
    """
    window_start = entry_day - timedelta(days=blackout_days_after)
    window_end = entry_day + timedelta(days=blackout_days_before)
    blackout = calendar.between(window_start, window_end, kinds)
    spanned = calendar.between(entry_day, expiration, kinds)
    return EventCheck(
        allowed=not blackout,
        blackout=blackout,
        spanned=spanned,
        calendar_loaded=calendar.has_dates(),
    )


def premium_compensates(credit: float, spot: float, n_events: int,
                        typical_event_move_pct: float = 0.01,
                        required_multiple: float = 1.0) -> tuple[bool, str]:
    """The framework's "unless the premium compensates" clause, made checkable.

    Compares the credit collected against the move the spanned events are
    expected to produce: `n_events * typical_event_move_pct * spot`, scaled
    by `required_multiple`. Returns (passes, explanation).

    `typical_event_move_pct` defaults to 1% of spot per major macro event,
    a placeholder the operator should replace with a measured figure for
    their own underlying -- the honest version of this number comes from
    the realized absolute move on past FOMC/CPI days, not from a default.
    Because the events are additive here and moves are not (they partly
    cancel), the test is deliberately conservative: it asks the credit to
    cover the WORST plausible sum, so passing it is a strong statement and
    failing it is only a caution.
    """
    if n_events <= 0:
        return True, "no spanned events to compensate for"
    required = n_events * typical_event_move_pct * spot * required_multiple
    ok = credit >= required
    verdict = "covers" if ok else "does NOT cover"
    return ok, (
        f"credit {credit:.2f} {verdict} the {n_events}-event budget "
        f"{required:.2f} ({typical_event_move_pct:.1%} of spot per event "
        f"x {required_multiple:g})"
    )
