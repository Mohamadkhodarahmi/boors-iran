from datetime import date, datetime

import pytest

from bors_analyser.features.fundamental import (
    event_features,
    growth,
    income_features,
    monthly_features,
    report_direction,
    ttm,
    valuation_features,
)

INCOME = {
    "period_end": "1405/03/31", "fiscal_year_end": "1405/12/29", "period_months": 3,
    "current": {"revenue": 834.0, "gross_profit": 232.0, "operating_profit": 198.0, "net_profit": 120.0, "eps": 63.0},
    "prior_same_period": {"revenue": 847.0, "gross_profit": 265.0, "operating_profit": 234.0, "net_profit": 161.0,
                          "eps": 84.0},
    "columns": [
        {"period_end": "1405/03/31", "items": {"net_profit": 120.0, "revenue": 834.0}},
        {"period_end": "1404/03/31", "items": {"net_profit": 161.0, "revenue": 847.0}},
        {"period_end": "1404/12/29", "items": {"net_profit": 1003.0, "revenue": 3668.0}},
    ],
}


def test_growth_handles_losses_and_zero():
    assert growth(120, 100) == pytest.approx(0.2)
    assert growth(-50, -100) == pytest.approx(0.5)  # loss halved -> improvement
    assert growth(10, 0) is None and growth(None, 5) is None


def test_ttm_uses_previous_fiscal_year_column():
    assert ttm(INCOME, "net_profit") == pytest.approx(1003 + 120 - 161)
    assert ttm(INCOME | {"period_months": 12}, "net_profit") == 120.0


def test_income_features():
    f = income_features(INCOME)
    assert f["revenue_growth"] == pytest.approx((834 - 847) / 847)
    assert f["profit_growth"] == pytest.approx((120 - 161) / 161)
    assert f["gross_margin"] == pytest.approx(232 / 834)
    assert f["gross_margin_change"] == pytest.approx(232 / 834 - 265 / 847)


def test_monthly_features_yoy_and_quarter():
    reports = []
    for y, base in ((2026, 120.0), (2025, 100.0)):
        for m in (8, 7, 6, 5):
            reports.append({"period_end": date(y, m, 22), "month_sales": base + m, "ytd_sales": None,
                            "prior_ytd_sales": None})
    reports[0].update(ytd_sales=500.0, prior_ytd_sales=400.0)
    reports.sort(key=lambda r: r["period_end"], reverse=True)
    f = monthly_features(reports)
    assert f["monthly_sales_yoy"] == pytest.approx((128 - 108) / 108)
    assert f["ytd_sales_growth"] == pytest.approx(0.25)
    assert f["quarterly_sales_growth"] == pytest.approx((128 + 127 + 126 - (108 + 107 + 106)) / (108 + 107 + 106))


def test_event_features_respects_asof_window():
    asof = datetime(2026, 9, 23, 12)
    letters = [
        {"title": "a", "report_type": "capital_increase", "event_tags": ["capital_increase"],
         "published_at": datetime(2026, 9, 20)},
        {"title": "future", "report_type": "major_disclosure", "event_tags": ["major_contract"],
         "published_at": datetime(2026, 9, 24)},
        {"title": "old", "report_type": "major_disclosure", "event_tags": ["auditor_issue"],
         "published_at": datetime(2026, 6, 1)},
    ]
    f = event_features(letters, asof, 30)
    assert f["has_capital_increase"] is True
    assert f["has_major_contract"] is False  # published after asof -> invisible
    assert f["has_auditor_issue"] is False  # outside the window
    assert f["recent_letters"] == 1


def test_report_direction_and_valuation():
    assert report_direction({"profit_growth": 0.6, "revenue_growth": 0.5})["recent_report_positive"] is True
    # 15% nominal growth is below the inflation hurdle -> not positive, not negative
    mild = report_direction({"profit_growth": 0.15, "revenue_growth": 0.15})
    assert mild == {"recent_report_positive": False, "recent_report_negative": False}
    assert report_direction({"profit_growth": -0.3})["recent_report_negative"] is True
    # sales up but deeper loss -> never positive
    loss = report_direction({"profit_growth": -1.35, "net_profit": -224.0, "revenue_growth": 0.24,
                             "ytd_sales_growth": 0.55, "monthly_sales_yoy": 0.61})
    assert loss["recent_report_positive"] is False and loss["recent_report_negative"] is True
    assert report_direction({})["recent_report_positive"] is False
    v = valuation_features(10_000 * 1e6, {"ttm_net_profit": 1000.0, "ttm_revenue": 5000.0, "equity": 2000.0})
    assert v["pe_ttm"] == pytest.approx(10) and v["ps_ttm"] == pytest.approx(2) and v["pb"] == pytest.approx(5)
    assert valuation_features(1e9, {"ttm_net_profit": -5.0})["pe_ttm"] is None
