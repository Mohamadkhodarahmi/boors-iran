from datetime import date, datetime, time

from bors_analyser.timeutil import (
    TEHRAN,
    deven_to_date,
    heven_to_time,
    in_session,
    jalali_to_date,
    jalali_to_datetime,
    normalize_fa,
)


def test_normalize_fa_digits_and_letters():
    assert normalize_fa("  ۱۴۰۵/۰۵/۳۱ ") == "1405/05/31"
    assert normalize_fa("شركت نفت ايران") == "شرکت نفت ایران"


def test_deven_heven():
    assert deven_to_date(20260923) == date(2026, 9, 23)
    assert heven_to_time(91503) == time(9, 15, 3)
    assert heven_to_time(123000) == time(12, 30, 0)


def test_jalali_conversion():
    assert jalali_to_date("1405/07/01") == date(2026, 9, 23)
    dt = jalali_to_datetime("۱۴۰۵/۰۶/۰۴ ۱۵:۲۳:۵۲")
    assert dt == datetime(2026, 8, 26, 15, 23, 52, tzinfo=TEHRAN)


def test_in_session():
    wed_10 = datetime(2026, 9, 23, 10, 0, tzinfo=TEHRAN)  # Wednesday
    thu_10 = datetime(2026, 9, 24, 10, 0, tzinfo=TEHRAN)  # Thursday (closed)
    wed_14 = datetime(2026, 9, 23, 14, 0, tzinfo=TEHRAN)
    assert in_session(wed_10)
    assert not in_session(thu_10)
    assert not in_session(wed_14)
