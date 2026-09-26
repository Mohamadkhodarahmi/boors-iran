"""Human-like Persian narrative built ONLY from the evidence dictionary.

Rules
- Every sentence is emitted by a template bound to specific evidence fields; if a field is missing
  the sentence is skipped (or says "data unavailable"). Nothing is invented.
- Observed facts ("مشاهده") and model interpretation ("تفسیر") are kept in separate sections.
- The final text passes through `guard.check` (no certainty/buy/sell language).
"""

from __future__ import annotations

from typing import Any

from bors_analyser.analysis import fmt
from bors_analyser.analysis.guard import DISCLAIMER, check

TREND_FA = {"up": "صعودی", "down": "نزولی", "sideways": "خنثی/رنج"}
REGIME_FA = {"rising": "افزایشی", "falling": "کاهشی", "flat": "ثابت", "jump": "تغییر ناگهانی",
             "insufficient_data": "داده ناکافی"}
RISK_FA = {
    "near_resistance": "قیمت نزدیک مقاومت ۲۰ روزه است",
    "overbought": "RSI در ناحیه اشباع خرید",
    "high_volatility": "نوسان‌پذیری بالا",
    "volatility_rising": "افزایش نوسان‌پذیری نسبت به میانگین ۶۰ روزه",
    "extended_above_ma": "فاصله زیاد قیمت از میانگین ۵۰ روزه",
    "chasing_after_spike": "رشد تند امروز؛ تعقیب قیمت ریسک بیشتری دارد",
    "money_outflow": "خروج پول حقیقی در ۵ روز اخیر",
    "high_leverage": "نسبت بدهی به حقوق مالکانه بالا",
    "negative_report": "آخرین گزارش‌های مالی ضعیف",
    "auditor_issue": "نشانه‌ای از اظهارنظر مشروط/منفی حسابرس در اطلاعیه‌ها",
    "trading_halt": "توقف/تعلیق نماد در اطلاعیه‌های اخیر",
    "capital_increase": "افزایش سرمایه در جریان (احتمال تعدیل قیمت)",
    "loss_making": "شرکت در آخرین دوره زیان‌ده بوده",
}


def _g(d: dict[str, Any] | None, *keys: str) -> Any:
    for k in keys:
        if d is None:
            return None
        d = d.get(k) if isinstance(d, dict) else None
    return d


# ----------------------------------------------------------------------------- sections


def tablo_lines(ev: dict[str, Any]) -> list[str]:
    t = ev.get("tablo") or {}
    intr = ev.get("intraday") or {}
    ob = ev.get("orderbook") or {}
    lines = []
    bp = t.get("buyer_power")
    if bp is not None:
        trend = _g(intr, "buyer_power", "change_30m")
        lines.append(f"قدرت خریدار: {fmt.ratio(bp)} {fmt.arrow(trend) if trend is not None else ''}".rstrip())
    if t.get("volume_ratio_20") is not None:
        lines.append(f"حجم: {fmt.ratio(t['volume_ratio_20'])} میانگین ۲۰ روزه")
    if t.get("relative_volume") is not None:
        lines.append(f"حجم نسبی تا این لحظه: {fmt.ratio(t['relative_volume'])}")
    if t.get("real_money_flow") is not None:
        lines.append(f"پول حقیقی: {fmt.money(t['real_money_flow'])}")
    if t.get("cum_real_money_flow_5") is not None:
        lines.append(f"پول حقیقی ۵ روز: {fmt.money(t['cum_real_money_flow_5'])}")
    if ob.get("buy_queue"):
        qs = ob.get("queue_strength")
        lines.append("صف خرید" + (f" ({fmt.ratio(qs)} ارزش معاملات روزانه)" if qs else ""))
    elif ob.get("sell_queue"):
        qs = ob.get("queue_strength")
        lines.append("صف فروش" + (f" ({fmt.ratio(qs)} ارزش معاملات روزانه)" if qs else ""))
    elif ob.get("bid_ask_value_imbalance") is not None:
        imb = ob["bid_ask_value_imbalance"]
        side = "تقاضا" if imb > 0.15 else "عرضه" if imb < -0.15 else "متعادل"
        lines.append(f"سفارش‌ها: غلبه {side}" if side != "متعادل" else "سفارش‌ها: متعادل")
    return lines or [fmt.UNAVAILABLE]


def technical_lines(ev: dict[str, Any]) -> list[str]:
    x = ev.get("technical") or {}
    lines = []
    if x.get("trend"):
        lines.append(f"روند: {TREND_FA.get(x['trend'], x['trend'])}")
    if x.get("rsi_14") is not None:
        lines.append(f"RSI: {x['rsi_14']:.0f}")
    if x.get("resistance_20") is not None:
        lines.append(f"مقاومت ۲۰ روزه: {fmt.num(x['resistance_20'])} ({fmt.pct(x.get('dist_to_resistance_20'), 1)})")
    if x.get("support_20") is not None:
        lines.append(f"حمایت ۲۰ روزه: {fmt.num(x['support_20'])}")
    if x.get("breakout_20"):
        lines.append("شکست سقف ۲۰ روزه")
    if x.get("volatility_20") is not None:
        lines.append(f"نوسان سالانه‌شده ۲۰ روزه: {fmt.pct(x['volatility_20'], signed=False)}")
    return lines or [fmt.UNAVAILABLE]


def fundamental_lines(ev: dict[str, Any]) -> list[str]:
    f = ev.get("fundamental") or {}
    dq = f.get("data_quality") or {}
    if dq.get("income_statement") == "unavailable" and dq.get("monthly_activity") == "unavailable":
        return [fmt.UNAVAILABLE + " (گزارش کدال پردازش‌شده‌ای موجود نیست)"]
    lines = []
    if f.get("period_months"):
        lines.append(f"آخرین صورت مالی: {f['period_months']} ماهه منتهی به {f.get('period_end')}")
    if f.get("revenue_growth") is not None:
        lines.append(f"رشد درآمد (نسبت به دوره مشابه): {fmt.pct(f['revenue_growth'])}")
    if f.get("profit_growth") is not None:
        lines.append(f"رشد سود خالص: {fmt.pct(f['profit_growth'])}")
    if f.get("net_margin") is not None:
        lines.append(f"حاشیه سود خالص: {fmt.pct(f['net_margin'], signed=False)}")
    if f.get("ytd_sales_growth") is not None:
        lines.append(f"رشد فروش تجمعی سال (گزارش ماهانه): {fmt.pct(f['ytd_sales_growth'])}")
    if f.get("monthly_sales_yoy") is not None:
        lines.append(f"فروش آخرین ماه نسبت به ماه مشابه پارسال: {fmt.pct(f['monthly_sales_yoy'])}")
    if f.get("pe_ttm") is not None:
        lines.append(f"P/E (TTM): {f['pe_ttm']:.1f}")
    status = ("مثبت" if f.get("recent_report_positive") else "ضعیف" if f.get("recent_report_negative")
              else "خنثی / کمتر از تورم")
    lines.append(f"وضعیت گزارش‌ها: {status}")
    if any(f.get(k) is not None for k in ("revenue_growth", "profit_growth", "ytd_sales_growth")):
        lines.append("(رشدها اسمی‌اند و اثر تورم از آن‌ها حذف نشده)")
    return lines


def interpretation(ev: dict[str, Any]) -> list[str]:
    """Interpretation sentences, each justified by specific evidence."""
    t, x, f = ev.get("tablo") or {}, ev.get("technical") or {}, ev.get("fundamental") or {}
    intr = ev.get("intraday") or {}
    out: list[str] = []

    vr, bp, flow = t.get("volume_ratio_20"), t.get("buyer_power"), t.get("real_money_flow")
    if vr is not None and vr >= 2 and flow is not None and flow > 0 and bp is not None and bp >= 1.5:
        out.append(f"حجم معاملات حدود {vr:.1f} برابر میانگین بوده و همزمان با ورود پول حقیقی، "
                   f"قدرت خریدار به {bp:.1f} رسیده است.")
    elif vr is not None and vr >= 2:
        out.append(f"حجم معاملات حدود {vr:.1f} برابر میانگین بوده است.")
    if flow is not None and bp is not None and bp >= 1.5 and flow < 0:
        out.append("قدرت خریدار بالاست اما در مجموع پول حقیقی خارج شده؛ یعنی تعداد کمتری خریدار با حجم بیشتر "
                   "در مقابل فروشندگان پرتعداد قرار گرفته‌اند.")
    elif flow is not None and flow < 0 and (bp is None or bp < 1):
        out.append("خروج پول حقیقی و ضعف سرانه خرید مشاهده می‌شود.")

    bp_int = intr.get("buyer_power") or {}
    if bp_int.get("regime") in ("rising", "falling", "jump") and bp_int.get("path"):
        first, last = bp_int["path"][0], bp_int["path"][-1]
        out.append(f"در طول جلسه قدرت خریدار روند {REGIME_FA[bp_int['regime']]} داشته "
                   f"(از {first[1]:.2f} در {first[0]} به {last[1]:.2f} در {last[0]}).")

    pg, msy = f.get("profit_growth"), f.get("monthly_sales_yoy")
    if pg is not None and msy is not None and pg < 0 < msy and msy > 0.3:
        out.append(f"تصویر بنیادی دوگانه است: سود آخرین صورت مالی {fmt.pct(pg)} تغییر کرده، اما فروش آخرین ماه "
                   f"نسبت به ماه مشابه پارسال {fmt.pct(msy)} بوده؛ گزارش‌های بعدی جهت را روشن‌تر می‌کنند.")

    div = ev.get("divergence")
    if div:
        out.append(div["fa"])
    elif f.get("recent_report_positive") and (t.get("real_money_flow") or 0) > 0:
        out.append("از طرف بنیادی، آخرین گزارش‌های شرکت با حرکت فعلی بازار تضاد شدیدی ندارند.")

    d_res = x.get("dist_to_resistance_20")
    if d_res is not None and 0 <= d_res < 0.02 and not x.get("breakout_20"):
        out.append("قیمت به محدوده مقاومت نزدیک است؛ تعقیب قیمت در این نقطه ریسک بیشتری دارد.")
    if x.get("breakout_20") and vr is not None and vr >= 1.5:
        out.append("قیمت سقف ۲۰ روزه را با حجم بالاتر از میانگین شکسته است؛ پایداری شکست در روزهای بعد تعیین‌کننده است.")

    for s in ev.get("setups") or []:
        r5, base5 = _g(s, "return_after_5d"), _g(s, "baseline_5d")
        if r5 and base5 and r5.get("n", 0) >= 30:
            out.append(
                f"در گذشته ({s['period'][0][:4]} تا {s['period'][1][:4]}) در {r5['n']} مورد مشابه «{s['fa']}»، "
                f"میانگین بازده ۵ روزه {fmt.pct(r5['mean'], 1)} با نرخ موفقیت {r5['win_rate'] * 100:.0f}٪ بوده؛ "
                f"در برابر میانگین کل بازار {fmt.pct(base5['mean'], 1)} و {base5['win_rate'] * 100:.0f}٪."
            )
            r1 = _g(s, "return_after_1d")
            if r1 and r1.get("mean", 0) < 0:
                out.append(f"در همین موارد، بازده روز اول پس از ورود به‌طور میانگین منفی ({fmt.pct(r1['mean'], 1)}) بوده است.")
    return out


def scenario(ev: dict[str, Any]) -> str | None:
    x = ev.get("technical") or {}
    t = ev.get("tablo") or {}
    res, sup = x.get("resistance_20"), x.get("support_20")
    if res and sup:
        return (f"در صورت حفظ تقاضا و تثبیت بالای {fmt.num(res)}، سناریوی ادامه حرکت قابل بررسی است؛ "
                f"از دست رفتن حمایت {fmt.num(sup)} این سناریو را تضعیف می‌کند.")
    if t.get("real_money_flow") is not None:
        return "ادامه یا توقف ورود پول حقیقی در روزهای آینده، معیار اصلی ارزیابی این وضعیت است."
    return None


def model_block(ev: dict[str, Any]) -> list[str]:
    sc = ev.get("scores") or {}
    lines = []
    if sc.get("overall_score") is not None:
        lines.append(f"Model Score: {sc['overall_score']:.0f}/100")
    for comp, fa in (("tablo_score", "تابلو"), ("technical_score", "تکنیکال"),
                     ("fundamental_score", "بنیادی"), ("risk_score", "ریسک")):
        if sc.get(comp) is not None:
            lines.append(f"{fa}: {sc[comp]:.0f}")
    model = ev.get("model") or {}
    shown = False
    for m in model.values():
        if m.get("calibrated") and m.get("probability") is not None:
            b = m.get("historical_bin") or {}
            lines.append(f"احتمال ({m['definition_fa']}): {m['probability'] * 100:.0f}٪")
            if b:
                lines.append(f"دقت تاریخی در این بازه پیش‌بینی: حدود {b['observed'] * 100:.0f}٪ (n={b['n']})")
            shown = True
    if not shown:
        lines.append("Probability: در حال calibration (عدد احتمال نمایش داده نمی‌شود)")
    return lines


def build_report(ev: dict[str, Any]) -> dict[str, Any]:
    """Structured sections + plain narrative. Everything passes the guard."""
    risks = [RISK_FA.get(r, r) for r in (ev.get("scores") or {}).get("risk_flags", [])]
    sections = {
        "tablo": tablo_lines(ev),
        "technical": technical_lines(ev),
        "fundamental": fundamental_lines(ev),
        "analysis": interpretation(ev),
        "risks": risks,
        "scenario": scenario(ev),
        "model": model_block(ev),
        "status": (ev.get("scores") or {}).get("fa"),
        "status_en": (ev.get("scores") or {}).get("label"),
        "disclaimer": DISCLAIMER,
    }
    narrative_parts = []
    if sections["analysis"]:
        narrative_parts.append("\n".join(sections["analysis"]))
    if risks:
        narrative_parts.append("ریسک‌ها: " + "؛ ".join(risks) + ".")
    if sections["status"]:
        narrative_parts.append(f"وضعیت فعلی: {sections['status']}")
    if sections["scenario"]:
        narrative_parts.append(f"سناریوی قابل بررسی: {sections['scenario']}")
    narrative = "\n\n".join(narrative_parts) or "داده کافی برای تحلیل وجود ندارد."
    check(narrative)
    for v in sections.values():
        for line in (v if isinstance(v, list) else [v]):
            if isinstance(line, str):
                check(line)
    return {"sections": sections, "narrative": narrative}


def telegram_text(ev: dict[str, Any], report: dict[str, Any], mode: str = "swing") -> str:
    """The Telegram card (plain text, fits one message)."""
    s = report["sections"]
    sc = ev.get("scores") or {}
    dot = {"Positive setup": "🟢", "Candidate": "🟢", "Watch": "🟡", "Neutral": "⚪", "Risky": "🟠",
           "Negative": "🔴"}.get(sc.get("label"), "⚪")
    bar = "━━━━━━━━━━━━━━━━━━"
    parts = [bar, f"{dot} {ev['symbol']} — {ev.get('name') or ''}".strip(" —"), bar, ""]
    if mode in ("short", "swing"):
        parts += ["📊 TABLO", *s["tablo"], ""]
    if mode == "swing":
        parts += ["📈 TECHNICAL", *s["technical"], ""]
    if mode in ("swing", "fundamental"):
        parts += ["🏢 FUNDAMENTAL", *s["fundamental"], ""]
    if s["analysis"]:
        parts += ["🧠 ANALYSIS", *s["analysis"], ""]
    if s["risks"]:
        parts += ["⚠️ RISKS", *[f"- {r}" for r in s["risks"]], ""]
    parts += [f"وضعیت: {s['status'] or 'داده ناکافی'}", ""]
    parts += ["🎯 MODEL", *s["model"], ""]
    parts += [bar, f"⏱ Data: {ev.get('data_time') or fmt.UNAVAILABLE}", bar, s["disclaimer"]]
    return check("\n".join(p for p in parts if p is not None))
