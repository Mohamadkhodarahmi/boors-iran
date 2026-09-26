import pytest

from bors_analyser.analysis import narrator
from bors_analyser.analysis.divergence import detect
from bors_analyser.analysis.guard import GuardViolation, check


def test_guard_blocks_certainty_but_allows_possibility():
    for bad in ["این سهم قطعاً رشد می‌کند", "BUY now", "سود تضمینی", "بدون ریسک", "حتما بخرید", "100% موفق"]:
        with pytest.raises(GuardViolation):
            check(bad)
    ok = "این وضعیت می‌تواند نشانه انباشت احتمالی باشد؛ احتمالاً نیازمند تأیید است."
    assert check(ok) == ok


def test_divergence_wording_is_hedged():
    d = detect({"tablo": "-", "fundamental": "+", "technical": "0"})
    assert d["code"] == "possible_accumulation" and "احتمالی" in d["fa"] and "تأیید نشده" in d["fa"]
    assert detect({"tablo": "+", "fundamental": "+", "technical": "+"})["code"] == "strong_alignment"
    assert detect({"tablo": "0", "fundamental": None, "technical": "0"}) is None


def _ev(**over):
    ev = {
        "symbol": "XYZ", "name": "شرکت نمونه", "data_time": "10:35:12",
        "tablo": {"buyer_power": 2.8, "volume_ratio_20": 3.7, "real_money_flow": 42e9 * 10},
        "technical": {"trend": "up", "rsi_14": 64, "resistance_20": 1000, "support_20": 900,
                      "dist_to_resistance_20": 0.01},
        "fundamental": {"revenue_growth": 0.18, "profit_growth": 0.24, "recent_report_positive": True,
                        "data_quality": {"income_statement": "ok", "monthly_activity": "ok"}},
        "scores": {"overall_score": 82, "tablo_score": 86, "risk_flags": ["near_resistance"],
                   "fa": "مثبت / نیازمند تأیید", "label": "Positive setup"},
        "model": {}, "setups": [], "divergence": None,
    }
    ev.update(over)
    return ev


def test_report_uses_only_present_fields():
    rep = narrator.build_report(_ev())
    text = "\n".join(rep["sections"]["analysis"])
    assert "3.7" in text and "2.8" in text
    assert "مقاومت" in text
    # nothing about intraday path or setups because they are not in the evidence
    assert "در طول جلسه" not in text and "در گذشته" not in text


def test_empty_evidence_says_unavailable_not_invented():
    ev = _ev(tablo={}, technical={}, fundamental={"data_quality": {"income_statement": "unavailable",
                                                                  "monthly_activity": "unavailable"}},
             scores={"overall_score": None, "risk_flags": []})
    rep = narrator.build_report(ev)
    assert rep["sections"]["tablo"] == ["داده در دسترس نیست"]
    assert rep["sections"]["fundamental"][0].startswith("داده در دسترس نیست")
    assert rep["sections"]["analysis"] == []


def test_no_probability_unless_calibrated():
    uncal = _ev(model={"hit3_3d": {"calibrated": False, "model_score": 61.0, "definition_fa": "x"}})
    lines = narrator.model_block(uncal)
    assert not any("٪" in line and "احتمال (" in line for line in lines)
    assert any("calibration" in line for line in lines)
    cal = _ev(model={"hit3_3d": {"calibrated": True, "probability": 0.72, "definition_fa": "رسیدن به ۳٪+",
                                 "historical_bin": {"observed": 0.7, "n": 1200, "predicted": 0.71}}})
    lines = narrator.model_block(cal)
    assert any("72٪" in line for line in lines) and any("n=1200" in line for line in lines)


def test_telegram_card_contains_sections_and_disclaimer():
    ev = _ev()
    txt = narrator.telegram_text(ev, narrator.build_report(ev), "swing")
    for part in ("📊 TABLO", "📈 TECHNICAL", "🏢 FUNDAMENTAL", "🧠 ANALYSIS", "⚠️ RISKS", "🎯 MODEL", "توصیه خرید یا فروش نیست"):
        assert part in txt
    short = narrator.telegram_text(ev, narrator.build_report(ev), "short")
    assert "🏢 FUNDAMENTAL" not in short


def test_setup_history_sentence_cites_counts_and_baseline():
    setup = {"id": "s", "fa": "حجم بالا", "period": ["2019-06-01", "2026-09-22"],
             "return_after_1d": {"mean": -0.003, "n": 3000}, "return_after_5d": {"n": 3195, "mean": 0.0117,
                                                                                 "win_rate": 0.54},
             "baseline_5d": {"mean": 0.0063, "win_rate": 0.51}}
    text = "\n".join(narrator.interpretation(_ev(setups=[setup])))
    assert "3195" in text and "54٪" in text and "51٪" in text and "روز اول" in text
