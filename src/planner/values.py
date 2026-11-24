"""Google Sheets value conversion and text safety.

Reads use UNFORMATTED_VALUE + SERIAL_NUMBER: dates are day counts since 1899-12-30,
times are fractions of a day. Writes use USER_ENTERED strings so Sheets stores real
dates and times.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from typing import Any

log = logging.getLogger("assistant")

EPOCH = dt.date(1899, 12, 30)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(text: str) -> str:
    return text.translate(_DIGITS)


# --- reading ---------------------------------------------------------------------

def to_date(value: Any) -> dt.date | None:
    """Serial number (or ISO text typed by a human) -> date."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return EPOCH + dt.timedelta(days=int(value))
    if isinstance(value, str):
        text = normalize_digits(value.strip().lstrip("'"))
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            log.warning("planner: unreadable date cell")
    return None


def to_minutes(value: Any) -> int | None:
    """Time cell (fraction of a day, or 'HH:MM' text) -> minutes since midnight (1440 = 24:00)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        frac = value if value <= 1 else value - int(value)
        if value >= 1 and frac == 0:
            return 1440
        return int(round(frac * 1440))
    if isinstance(value, str):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", normalize_digits(value.strip()))
        if m:
            return int(m.group(1)) * 60 + int(m.group(2))
        log.warning("planner: unreadable time cell")
    return None


def to_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        day = to_date(value)
        minutes = int(round((value - int(value)) * 1440))
        return dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=minutes)
    day = to_date(value)
    return dt.datetime.combine(day, dt.time()) if day else None


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def to_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(float(normalize_digits(str(value))))
    except ValueError:
        return None


def to_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(normalize_digits(str(value)).replace(",", ""))
    except ValueError:
        return None


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().upper() in ("TRUE", "FALSE"):
        return value.strip().upper() == "TRUE"
    return None


# --- writing / display ------------------------------------------------------------

def fmt_date(day: dt.date) -> str:
    return day.isoformat()


def fmt_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_datetime(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def parse_hhmm(text: str) -> int:
    hours, minutes = normalize_digits(text).split(":")
    return int(hours) * 60 + int(minutes)


def display(value: Any) -> str:
    """Human display string used in ChangeLog Old/New values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        return fmt_datetime(value)
    if isinstance(value, dt.date):
        return fmt_date(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


_NUMBER_LIKE = re.compile(r"^[\s]*[-+]?(\d[\d,]*\.?\d*|\.\d+)\s*%?$")
_DATE_LIKE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}|^\d{1,2}/\d{1,2}(/\d{2,4})?$|^\d{1,2}:\d{2}")


def sanitize_text(text: Any, max_len: int) -> str:
    """Free text -> safe USER_ENTERED cell content.

    Guards against formula injection (=, +, -, @) and stops text that looks like a
    number, date or time from being converted by Sheets.
    """
    out = unicodedata.normalize("NFC", str(text or ""))
    out = re.sub(r"\s+", " ", out.replace("\t", " ").replace("\n", " ")).strip()
    out = out[:max_len]
    if out and (out[0] in "=+-@" or _NUMBER_LIKE.match(out) or _DATE_LIKE.match(normalize_digits(out))):
        out = "'" + out
    return out
