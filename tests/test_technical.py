from datetime import date, timedelta

import polars as pl
import pytest

from bors_analyser.features.technical import technical_panel


def _series(closes, code="A", adj=None):
    d0 = date(2025, 1, 1)
    return pl.DataFrame([
        {"ins_code": code, "date": d0 + timedelta(days=i), "open": c, "high": c * 1.01, "low": c * 0.99,
         "close": c, "adj_close": (adj[i] if adj else c)}
        for i, c in enumerate(closes)
    ])


def test_rsi_extremes_and_trend_up():
    up = technical_panel(_series([100 + i for i in range(260)])).row(-1, named=True)
    assert up["rsi_14"] == pytest.approx(100.0)
    assert up["trend"] == "up"
    assert up["sma_20"] == pytest.approx(sum(100 + i for i in range(240, 260)) / 20)
    down = technical_panel(_series([400 - i for i in range(260)])).row(-1, named=True)
    assert down["rsi_14"] < 5 and down["trend"] == "down"


def test_breakout_uses_previous_levels_only():
    closes = [100.0] * 40 + [110.0]
    last = technical_panel(_series(closes)).row(-1, named=True)
    assert last["resistance_20"] == pytest.approx(101.0)  # previous highs, not today's
    assert last["breakout_20"] is True
    flat = technical_panel(_series([100.0] * 41)).row(-1, named=True)
    assert flat["breakout_20"] is False


def test_adjustment_removes_capital_increase_gap():
    # raw close halves after a 100% capital increase; adjusted series is continuous
    raw = [100.0] * 30 + [50.0] * 30
    adj = [50.0] * 60
    last = technical_panel(_series(raw, adj=adj)).row(-1, named=True)
    assert last["breakdown_20"] is False
    assert last["volatility_20"] == pytest.approx(0.0)


def test_instruments_do_not_leak_into_each_other():
    a = _series([100.0 + i for i in range(60)], "A")
    b = _series([10.0] * 60, "B")
    out = technical_panel(pl.concat([a, b]))
    first_b = out.filter(pl.col("ins_code") == "B").row(25, named=True)
    assert first_b["sma_20"] == pytest.approx(10.0)
