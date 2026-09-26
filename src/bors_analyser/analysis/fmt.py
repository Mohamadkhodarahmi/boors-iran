"""Formatting helpers for Persian output (Latin digits, explicit units)."""

from __future__ import annotations

from bors_analyser.config import get_scoring_config

UNAVAILABLE = "داده در دسترس نیست"


def money(rial: float | None, signed: bool = True) -> str:
    """Rial amount -> short string in the configured display currency, e.g. '+4.2 همت' / '+420 میلیارد تومان'."""
    if rial is None:
        return UNAVAILABLE
    currency = (get_scoring_config().get("display") or {}).get("currency", "toman")
    v = rial / 10 if currency == "toman" else rial
    unit = "تومان" if currency == "toman" else "ریال"
    sign = ("+" if v > 0 else "−" if v < 0 else "") if signed else ("−" if v < 0 else "")
    a = abs(v)
    if a >= 1e12:
        s = f"{a / 1e12:.1f} هزار میلیارد {unit}"
    elif a >= 1e9:
        s = f"{a / 1e9:.0f} میلیارد {unit}" if a >= 1e11 else f"{a / 1e9:.1f} میلیارد {unit}"
    elif a >= 1e6:
        s = f"{a / 1e6:.0f} میلیون {unit}"
    else:
        s = f"{a:,.0f} {unit}"
    return sign + s


def million_rial(v: float | None) -> str:
    """CODAL amounts are million Rial."""
    return UNAVAILABLE if v is None else money(v * 1_000_000, signed=False)


def pct(x: float | None, digits: int = 0, signed: bool = True) -> str:
    if x is None:
        return UNAVAILABLE
    v = x * 100
    s = f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"
    return s.replace("-", "−")


def ratio(x: float | None, digits: int = 1) -> str:
    return UNAVAILABLE if x is None else f"{x:.{digits}f}x"


def num(x: float | None, digits: int = 0) -> str:
    return UNAVAILABLE if x is None else f"{x:,.{digits}f}"


def arrow(change: float | None, eps: float = 1e-9) -> str:
    if change is None:
        return ""
    return "↑" if change > eps else "↓" if change < -eps else "→"
