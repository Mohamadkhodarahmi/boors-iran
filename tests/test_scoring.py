import polars as pl
import pytest

from bors_analyser.config import PROJECT_ROOT
from bors_analyser.decision.scoring import component_score, market_features, risk_score, score_all, sub_score


@pytest.fixture
def cfg():
    import yaml
    return yaml.safe_load((PROJECT_ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8"))


def test_sub_score_direction_and_clipping():
    assert sub_score(2.0, 1.0, 3.0) == 50
    assert sub_score(10.0, 1.0, 3.0) == 100
    assert sub_score(4.0, 15.0, 4.0) == 100  # inverted: low P/E is good
    assert sub_score(20.0, 15.0, 4.0) == 0


def test_component_score_reports_coverage_and_skips_missing():
    spec = {"a": {"weight": 1, "lo": 0, "hi": 1}, "b": {"weight": 3, "lo": 0, "hi": 1}}
    out = component_score({"a": 1.0, "b": None}, spec)
    assert out["score"] == 100 and out["coverage"] == 0.25
    assert component_score({}, spec)["score"] is None


def test_risk_flags(cfg):
    r = risk_score({"rsi_14": 80, "dist_to_resistance_20": 0.01, "has_auditor_issue": True,
                    "recent_report_negative": False}, cfg["risk"])
    assert set(r["flags"]) == {"overbought", "near_resistance", "auditor_issue"}
    assert r["score"] == 50


def test_score_all_never_returns_probability(cfg):
    feats = {"volume_ratio_20": 3.5, "real_money_flow_ratio": 0.3, "buyer_power": 2.8, "rsi_14": 60,
             "momentum_20": 0.1, "profit_growth": 0.9, "market_breadth": 0.6}
    s = score_all(feats, cfg, "swing")
    assert s["score_kind"] == "model_score"
    assert 0 <= s["overall_score"] <= 100
    assert s["directions"]["tablo"] == "+"
    assert "probability" not in s
    empty = score_all({}, cfg, "swing")
    assert empty["overall_score"] is None and empty["label"] == "Insufficient data"


def test_market_features():
    df = pl.DataFrame({
        "date": ["d1"] * 4, "avg_value_20": [1e10, 1e10, 1e10, 1.0],
        "close_return": [0.01, -0.02, 0.03, 0.5], "real_money_flow": [10.0, -5.0, 5.0, 100.0],
        "value": [100.0, 100.0, 100.0, 1.0],
    })
    m = market_features(df).row(0, named=True)
    assert m["market_breadth"] == pytest.approx(2 / 3)  # illiquid stock excluded
    assert m["market_money_flow_ratio"] == pytest.approx(10 / 300)
