from datetime import date, datetime, timedelta

import polars as pl
import pytest

from bors_analyser.features.tablo import (
    daily_tablo_panel,
    intraday_relative_volume,
    orderbook_features,
    queue_strength,
)
from bors_analyser.features.timeseries import classify_regime, intraday_behaviour


def _daily(n: int = 40, spike_last: bool = True) -> pl.DataFrame:
    rows = []
    d0 = date(2026, 1, 1)
    for i in range(n):
        vol = 1000.0 * (3 if (spike_last and i == n - 1) else 1)
        close = 100.0 + i
        rows.append({
            "ins_code": "A", "date": d0 + timedelta(days=i), "open": close - 1, "high": close + 1,
            "low": close - 2, "close": close, "last": close, "yesterday": close - 1, "volume": vol,
            "value": vol * close, "trade_count": 10, "adj_close": None,
            "real_buy_value": 600.0 * close, "real_sell_value": 400.0 * close,
            "real_buy_count": 2, "real_sell_count": 4,
        })
    return pl.DataFrame(rows)


def test_volume_ratio_excludes_current_day():
    out = daily_tablo_panel(_daily()).filter(pl.col("ins_code") == "A")
    last = out.row(-1, named=True)
    assert last["volume_ratio_20"] == pytest.approx(3.0)
    assert last["volume_ratio_5"] == pytest.approx(3.0)


def test_money_flow_and_buyer_power():
    last = daily_tablo_panel(_daily()).row(-1, named=True)
    close = last["close"]
    assert last["real_money_flow"] == pytest.approx(200.0 * close)
    # (600c/2) / (400c/4) = 3
    assert last["buyer_power"] == pytest.approx(3.0)
    assert last["inflow_days_5"] == 5


def test_no_lookahead_future_rows_do_not_change_past_features():
    full = daily_tablo_panel(_daily(40))
    truncated = daily_tablo_panel(_daily(40).head(30))
    cols = ["volume_ratio_20", "buyer_power_prev5", "cum_real_money_flow_5", "momentum_20", "volume_zscore_60"]
    a = full.filter(pl.col("date") == date(2026, 1, 30)).select(cols).row(0)
    b = truncated.filter(pl.col("date") == date(2026, 1, 30)).select(cols).row(0)
    assert a == b


def test_missing_client_type_gives_null_not_zero():
    df = _daily().with_columns(pl.lit(None, dtype=pl.Float64).alias("real_buy_value"))
    last = daily_tablo_panel(df).row(-1, named=True)
    assert last["real_money_flow"] is None and last["buyer_power"] is None


def test_orderbook_buy_queue_detection():
    snap = {
        "bid_prices": [110, 109, 108, 107, 106], "bid_volumes": [1_000_000, 10, 10, 10, 10],
        "bid_counts": [500, 1, 1, 1, 1], "ask_prices": [0, 0, 0, 0, 0], "ask_volumes": [0, 0, 0, 0, 0],
        "ask_counts": [0, 0, 0, 0, 0], "price_limit_high": 110, "price_limit_low": 90,
    }
    ob = orderbook_features(snap)
    assert ob["buy_queue"] is True and ob["sell_queue"] is False
    assert ob["bid_ask_volume_imbalance"] == 1.0
    assert ob["bid_concentration"] > 0.99
    assert queue_strength(ob["buy_queue_value"], 55_000_000) == pytest.approx(ob["buy_queue_value"] / 55e6)


def test_orderbook_normal_book_spread():
    snap = {
        "bid_prices": [100, 99], "bid_volumes": [10, 10], "bid_counts": [1, 1],
        "ask_prices": [101, 102], "ask_volumes": [30, 30], "ask_counts": [3, 3],
        "price_limit_high": 110, "price_limit_low": 90,
    }
    ob = orderbook_features(snap)
    assert ob["spread"] == pytest.approx(1 / 100.5)
    assert ob["bid_ask_volume_imbalance"] == pytest.approx(-0.5)
    assert ob["buy_queue"] is False


def test_relative_volume_paces_by_session_time():
    ts = datetime(2026, 9, 23, 10, 45)  # 105 of 210 minutes
    assert intraday_relative_volume(500, 1000, ts) == pytest.approx(1.0)


def test_regime_classification():
    assert classify_regime([1.0, 1.2, 1.5, 2.0, 2.8]) == "rising"
    assert classify_regime([2.0, 1.8, 1.5, 1.2]) == "falling"
    assert classify_regime([1.0, 1.01, 1.0, 1.005]) == "flat"
    assert classify_regime([1.0, 1.01, 1.02, 1.03, 2.5]) == "jump"
    assert classify_regime([1.0]) == "insufficient_data"


def test_intraday_behaviour_buyer_power_path():
    base = datetime(2026, 9, 23, 10, 0)
    snaps = []
    for i, bp in enumerate([1.1, 1.5, 2.0, 2.8]):
        # buyer power = (rbv/rbc)/(rsv/rsc) with rbc=rsc=1 -> rbv/rsv
        snaps.append({"ts": base + timedelta(minutes=15 * i), "last": 100 + i, "volume": 1000.0 * (i + 1),
                      "value": 100_000.0 * (i + 1), "trade_count": 10 * (i + 1),
                      "real_buy_volume": bp * 100, "real_buy_count": 1, "real_sell_volume": 100.0,
                      "real_sell_count": 1})
    out = intraday_behaviour(snaps)
    assert out["buyer_power"]["current"] == pytest.approx(2.8)
    assert out["buyer_power"]["change_15m"] == pytest.approx(0.8)
    assert out["buyer_power"]["regime"] == "rising"
