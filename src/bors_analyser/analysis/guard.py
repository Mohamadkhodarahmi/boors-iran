"""Language guard: no certainty claims, no buy/sell orders, no guarantees.

Applied to every generated text (including any future LLM rephrasing). Violations raise, so a bad
template fails loudly in tests instead of reaching users.
"""

from __future__ import annotations

import re

BANNED = [
    r"\bBUY\b", r"\bSELL\b", r"\bguarantee", r"100\s*%", r"risk[- ]?free",
    # word boundaries matter: "حتما" is a substring of "احتمال" (possibility), which is allowed
    r"\bقطعی\b", r"\bقطعاً?\b", r"تضمین", r"بدون\s*ریسک", r"\bبی[\s‌]?ریسک\b", r"\bحتماً?\b",
    r"\bبخرید\b", r"\bبفروشید\b", r"سیگنال\s*(خرید|فروش)", r"صددرصد",
]
_BANNED_RE = re.compile("|".join(BANNED), re.IGNORECASE)

DISCLAIMER = "این تحلیل خودکار و مبتنی بر داده است و توصیه خرید یا فروش نیست."


class GuardViolation(ValueError):
    pass


def check(text: str) -> str:
    m = _BANNED_RE.search(text)
    if m:
        raise GuardViolation(f"banned phrase in generated text: {m.group(0)!r}")
    return text
