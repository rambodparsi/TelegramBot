"""Planner domain rules (spec section 5). Pure functions over already-loaded data."""

from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

from planner.model import Event, Settings

FOCUS_TYPES = ("Focus", "Task block")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Touching blocks (one ends when the other starts) do not overlap."""
    return a_start < b_end and b_start < a_end


def conflicts(events: Iterable[Event], day: dt.date, start: int, end: int, exclude_id: str | None = None) -> list[Event]:
    return [e for e in events if e.active and e.date == day and e.id != exclude_id
            and overlaps(start, end, e.start, e.end)]


def focus_minutes(events: Iterable[Event], day: dt.date, exclude_id: str | None = None) -> int:
    return sum(e.minutes for e in events if e.active and e.date == day and e.type in FOCUS_TYPES
               and e.id != exclude_id)


def routine_runs_on(days_text: str, weekday: int) -> bool | None:
    """weekday: 0=Mon. Returns None when the Days text is invalid."""
    text = (days_text or "").strip().lower()
    if text == "daily":
        return True
    if text == "weekdays":
        return weekday < 5
    if text == "weekends":
        return weekday >= 5
    tokens = [t for t in re.split(r"[,\s]+", text) if t]
    if not tokens or any(t[:3] not in _WEEKDAYS for t in tokens):
        return None
    return _WEEKDAYS[weekday] in {t[:3] for t in tokens}


def next_id(prefix: str, existing: Iterable[str], width: int) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    highest = max((int(m.group(1)) for i in existing if (m := pattern.match(i or ""))), default=0)
    return f"{prefix}-{highest + 1:0{width}d}"


def _round_up(minutes: int, step: int) -> int:
    return -(-minutes // step) * step


def find_free_slots(day_events: Iterable[Event], day: dt.date, minutes: int, area: str | None,
                    settings: Settings, now: dt.datetime, earliest: int | None = None,
                    latest: int | None = None, limit: int = 3) -> list[tuple[int, int]]:
    """Free (start, end) candidates on ``day`` for a block of ``minutes`` (spec 5.6)."""
    lo = max(settings.day_start, earliest if earliest is not None else 0)
    hi = min(settings.day_end, latest if latest is not None else 1440)
    if day == now.date():
        lo = max(lo, _round_up(now.hour * 60 + now.minute, 15))
    if day < now.date() or hi - lo < minutes:
        return []
    buf = settings.buffer_minutes
    busy = sorted((max(lo, e.start - buf), min(hi, e.end + buf)) for e in day_events
                  if e.active and e.date == day)
    merged: list[list[int]] = []
    for s, e in busy:
        if s >= e:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    gaps, cursor = [], lo
    for s, e in merged:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < hi:
        gaps.append((cursor, hi))

    candidates: list[int] = []
    for g_start, g_end in gaps:
        start = _round_up(g_start, 15)
        while start + minutes <= g_end:
            candidates.append(start)
            start += 30
    work_lo, work_hi = settings.work_hours
    weekday = day.weekday() < 5

    def in_work(start: int) -> bool:
        return work_lo <= start and start + minutes <= work_hi

    if weekday and area == "Work":
        key = lambda s: (not in_work(s), s)  # noqa: E731
    elif weekday:
        key = lambda s: (in_work(s) or overlaps(s, s + minutes, work_lo, work_hi), s)  # noqa: E731
    else:
        key = lambda s: (s < 9 * 60, s)  # noqa: E731
    chosen: list[tuple[int, int]] = []
    for start in sorted(candidates, key=key):
        if all(not overlaps(start, start + minutes, c[0], c[1]) for c in chosen):
            chosen.append((start, start + minutes))
        if len(chosen) == limit:
            break
    return chosen
