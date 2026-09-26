from datetime import date, timedelta

import polars as pl
import pytest

from bors_analyser.backtest.engine import condition_study, conditions_expr, parse_conditions
from bors_analyser.backtest.labels import add_forward_labels, target_hit, target_return


def _panel(closes, code="A", opens=None, highs=None, lows=None):
    d0 = date(2025, 1, 1)
    rows = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        rows.append({
            "ins_code": code, "date": d0 + timedelta(days=i), "open": o, "high": highs[i] if highs else max(o, c) + 1,
            "low": lows[i] if lows else min(o, c) - 1, "close": c, "yesterday": closes[i - 1] if i else c,
            "adj_close": c,
        })
    return pl.DataFrame(rows)


def test_entry_is_next_day_open_and_returns():
    df = add_forward_labels(_panel([100, 100, 110, 120, 130, 140], opens=[100, 102, 105, 115, 125, 135]))
    r0 = df.row(0, named=True)
    assert r0["entry_price"] == 102  # open of day 1
    assert r0["ret_1d"] == pytest.approx(100 / 102 - 1)  # close of day 1
    assert r0["ret_3d"] == pytest.approx(120 / 102 - 1)
    assert r0["max_gain_3d"] == pytest.approx(121 / 102 - 1)
    # last rows have no complete future window
    assert df.row(-1, named=True)["ret_1d"] is None
    assert df.row(-3, named=True)["ret_3d"] is None


def test_locked_limit_up_entry_is_unfillable():
    closes = [100, 103, 106]
    df = add_forward_labels(_panel(closes, opens=[100, 103, 106], highs=[101, 103, 107], lows=[99, 103, 105]))
    assert df.row(0, named=True)["entry_unfillable"] is True
    assert df.row(1, named=True)["entry_unfillable"] is False


def test_targets():
    df = add_forward_labels(_panel([100, 100, 104, 100, 100, 100], opens=[100] * 6)).with_columns(
        target_hit(0.03, 3).alias("hit"), target_return(0.05, 3).alias("r5"))
    assert df.row(0, named=True)["hit"] == 1 and df.row(0, named=True)["r5"] == 0


def test_parse_conditions():
    conds = parse_conditions("volume_ratio_20>3, buyer_power>=2, trend==up, breakout_20==true")
    assert conds == [("volume_ratio_20", ">", 3.0), ("buyer_power", ">=", 2.0), ("trend", "==", "up"),
                     ("breakout_20", "==", True)]
    with pytest.raises(ValueError):
        parse_conditions("volume_ratio_20 ~ 3")


def test_condition_study_compares_with_baseline():
    rows = []
    d0 = date(2025, 1, 1)
    for code, drift in (("A", 2.0), ("B", -1.0)):
        for i in range(60):
            c = 100 + drift * i
            rows.append({"ins_code": code, "date": d0 + timedelta(days=i), "open": c, "high": c + 0.5, "low": c - 0.5,
                         "close": c, "yesterday": c - drift, "adj_close": c, "avg_value_20": 1e10,
                         "flag": 1.0 if code == "A" else 0.0})
    panel = add_forward_labels(pl.DataFrame(rows))
    res = condition_study(panel, parse_conditions("flag>0.5"), cooldown=0, min_avg_value=1e9)
    sig, base = res["signal"]["return_after_5d"], res["baseline"]["return_after_5d"]
    assert sig["win_rate"] == 1.0 and base["win_rate"] == pytest.approx(0.5)
    assert sig["excess_mean_vs_baseline"] > 0
    res_cd = condition_study(panel, parse_conditions("flag>0.5"), cooldown=5, min_avg_value=1e9)
    assert res_cd["signal"]["sample_count"] < res["signal"]["sample_count"]


def test_null_features_never_match():
    df = pl.DataFrame({"x": [None, 5.0]})
    assert df.filter(conditions_expr([("x", ">", 1.0)])).height == 1
