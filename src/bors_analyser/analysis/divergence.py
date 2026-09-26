"""Alignment / divergence between market behaviour, fundamentals and technicals.

Directions come from component scores ('+', '0', '-', None). The wording is always a possibility,
never a conclusion.
"""

from __future__ import annotations

from typing import Any


def detect(directions: dict[str, str | None]) -> dict[str, Any] | None:
    t, f, x = directions.get("tablo"), directions.get("fundamental"), directions.get("technical")
    if t == "+" and f == "+" and x == "+":
        return {"code": "strong_alignment",
                "fa": "هم‌جهتی شواهد: رفتار تابلو، داده‌های بنیادی و وضعیت تکنیکال هر سه مثبت‌اند."}
    if t == "-" and f == "-" and x == "-":
        return {"code": "negative_alignment",
                "fa": "هم‌جهتی منفی: تابلو، بنیاد و تکنیکال هر سه ضعیف‌اند."}
    if t == "+" and f == "-":
        return {"code": "market_fundamental_divergence",
                "fa": "واگرایی بازار و بنیاد: رفتار تابلو مثبت است اما گزارش‌های اخیر شرکت ضعیف‌اند؛ "
                      "احتمال حرکت هیجانی یا خبری را باید در نظر گرفت."}
    if t == "-" and f == "+":
        return {"code": "possible_accumulation",
                "fa": "رفتار بازار منفی است در حالی که داده‌های بنیادی مثبت‌اند؛ این وضعیت می‌تواند نشانه "
                      "کم‌توجهی بازار یا انباشت احتمالی باشد، اما هنوز تأیید نشده است."}
    if t == "+" and x == "-":
        return {"code": "inflow_in_downtrend",
                "fa": "ورود پول در روند نزولی: ممکن است یک بازگشت کوتاه‌مدت باشد و تغییر روند هنوز تأیید نشده."}
    if t == "-" and x == "+":
        return {"code": "trend_without_money",
                "fa": "روند قیمت صعودی است اما پول حقیقی از سهم خارج می‌شود؛ ادامه روند نیازمند بازگشت تقاضاست."}
    return None
