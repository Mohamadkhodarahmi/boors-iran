"""Tehran time, Jalali dates and trading-session helpers."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import jdatetime

TEHRAN = ZoneInfo("Asia/Tehran")

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ARABIC_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی"})

# Saturday..Wednesday are trading days in Iran (Python weekday: Mon=0 .. Sun=6).
TRADING_WEEKDAYS = {5, 6, 0, 1, 2}


def now_tehran() -> datetime:
    return datetime.now(TEHRAN)


def normalize_fa(text: str | None) -> str:
    """Normalize Arabic letters and Persian/Arabic digits; strip zero-width marks."""
    if not text:
        return ""
    text = text.translate(_PERSIAN_DIGITS).translate(_ARABIC_LETTERS)
    text = text.replace("‏", "").replace("‎", "")
    return re.sub(r"\s+", " ", text).strip()


def deven_to_date(d: int | str) -> date:
    """TSETMC dEven (YYYYMMDD int) -> date."""
    s = str(int(d))
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def heven_to_time(h: int | str) -> time:
    """TSETMC hEven (HMMSS / HHMMSS int) -> time."""
    s = f"{int(h):06d}"
    return time(int(s[:2]), int(s[2:4]), int(s[4:6]))


def jalali_to_date(text: str) -> date:
    """'1405/05/31' or '۱۴۰۵/۰۵/۳۱' (optionally with a time part) -> Gregorian date."""
    t = normalize_fa(text).split(" ")[0].replace("-", "/")
    y, m, d = (int(x) for x in t.split("/"))
    return jdatetime.date(y, m, d).togregorian()


def jalali_to_datetime(text: str) -> datetime:
    """'1405/06/04 15:23:52' -> aware Tehran datetime."""
    t = normalize_fa(text)
    parts = t.split(" ")
    d = jalali_to_date(parts[0])
    hh, mm, ss = 0, 0, 0
    if len(parts) > 1:
        tp = [int(x) for x in parts[1].split(":")]
        hh, mm = tp[0], tp[1]
        ss = tp[2] if len(tp) > 2 else 0
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=TEHRAN)


def to_jalali_str(d: date) -> str:
    return jdatetime.date.fromgregorian(date=d).strftime("%Y/%m/%d")


def is_trading_day(d: date) -> bool:
    """Weekday check only; official holidays are detected from data (no trades that day)."""
    return d.weekday() in TRADING_WEEKDAYS


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_session(now: datetime | None = None, start: str = "08:55", end: str = "12:35") -> bool:
    now = (now or now_tehran()).astimezone(TEHRAN)
    return is_trading_day(now.date()) and parse_hhmm(start) <= now.time() <= parse_hhmm(end)
