"""A small five-field cron parser for the full-backup schedule.

Supported: ``minute hour day-of-month month day-of-week`` with ``*``, numbers,
ranges ``a-b``, lists ``a,b``, steps ``*/n`` and ``a-b/n``, and the shortcuts
``@hourly``, ``@daily``/``@midnight``, ``@weekly``, ``@monthly``. Day of week is
0-7 (0 and 7 are Sunday). As in classic cron, when both day-of-month and
day-of-week are restricted a day matches if either matches.

Times are evaluated in the timezone of the datetime passed in (the scheduler
passes container local time, which is UTC unless ``TZ`` is set).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_SHORTCUTS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))
# Upper bound on the search; every valid expression matches within ~4 years (Feb 29).
_MAX_SEARCH_DAYS = 366 * 5


class CronError(ValueError):
    """The expression is not a supported cron schedule."""


def _parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"empty {name} list item")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{name} step must be a positive number")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            if not (start_text.isdigit() and end_text.isdigit()):
                raise CronError(f"{name} range must be numeric")
            start, end = int(start_text), int(end_text)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = high
        else:
            raise CronError(f"{name} value {part!r} is not a number")
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise CronError(f"{name} must be between {low} and {high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        text = " ".join(str(expression or "").split())
        expanded = _SHORTCUTS.get(text.lower(), text)
        parts = expanded.split(" ")
        if len(parts) != 5:
            raise CronError("a schedule needs five fields: minute hour day-of-month month day-of-week")
        parsed = [_parse_field(part, *spec) for part, spec in zip(parts, _FIELDS)]
        weekdays = frozenset(day % 7 for day in parsed[4])
        return cls(
            expression=text,
            minutes=parsed[0],
            hours=parsed[1],
            days=parsed[2],
            months=parsed[3],
            weekdays=weekdays,
            day_restricted=parts[2] != "*",
            weekday_restricted=parts[4] != "*",
        )

    def _day_matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        day_ok = moment.day in self.days
        # Python: Monday=0; cron: Sunday=0.
        weekday_ok = (moment.weekday() + 1) % 7 in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def next_after(self, moment: datetime) -> datetime:
        """The first matching minute strictly after ``moment`` (same tzinfo)."""

        candidate = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_MAX_SEARCH_DAYS):
            if self._day_matches(candidate):
                for hour in sorted(self.hours):
                    if hour < candidate.hour:
                        continue
                    for minute in sorted(self.minutes):
                        if hour == candidate.hour and minute < candidate.minute:
                            continue
                        return candidate.replace(hour=hour, minute=minute)
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
        raise CronError("the schedule never matches a real date")
