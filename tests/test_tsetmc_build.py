from datetime import date, datetime

import polars as pl

from bors_analyser.collectors.tsetmc import (
    build_client_type_rows,
    build_daily_price_rows,
    build_eod_rows_from_snapshots,
    build_instrument_rows,
    build_snapshot_rows,
)
from bors_analyser.db.models import IntradaySnapshot

NOW = datetime(2026, 9, 23, 14, 0)


def _prices():
    row = {
        "ins_code": "46348559193224090", "isin": "IRO1FOLD0001", "l18": "فولاد", "l30": "فولاد مبارکه اصفهان",
        "heven": 123000, "pf": 3350, "pc": 3350, "pl": 3350, "tno": 21174, "tvol": 2210827100,
        "tval": 7398241941410, "pmin": 3280, "pmax": 3350, "py": 3260, "eps": 518.0, "bvol": 1,
        "flow": 1, "cs": "27", "tmax": 3350.0, "tmin": 3170.0, "z": 1935000000000, "yval": "300",
        "cgrvalcot": "N1",
    }
    for i in range(1, 6):
        row |= {f"pd{i}": 3350 - 10 * (i - 1), f"qd{i}": 1000 * i, f"zd{i}": i,
                f"po{i}": 3360 + 10 * (i - 1), f"qo{i}": 500 * i, f"zo{i}": i}
    etf = dict(row, ins_code="1", l18="اهرم", yval="305", cs="68")
    return pl.DataFrame([row, etf])


def test_instrument_rows_flags_stocks_and_sectors():
    rows = build_instrument_rows(_prices(), {"27": "فلزات اساسی"}, ["300", "303", "309"], NOW)
    folad = rows[0]
    assert folad["symbol"] == "فولاد" and folad["is_stock"] is True
    assert folad["sector_code"] == "27" and folad["sector_name"] == "فلزات اساسی"
    assert folad["market"] == "بورس"
    assert rows[1]["is_stock"] is False


def test_snapshot_rows_merge_client_type_and_order_book():
    ct = pl.DataFrame([{
        "ins_code": "46348559193224090", "n_buy_count": 2232, "l_buy_count": 24, "n_buy_volume": 1345256558,
        "l_buy_volume": 865570542, "n_sell_count": 3000, "l_sell_count": 10, "n_sell_volume": 1900000000,
        "l_sell_volume": 310827100,
    }])
    rows = build_snapshot_rows(_prices(), ct, NOW, date(2026, 9, 23))
    r = rows[0]
    assert r["close"] == 3350 and r["yesterday"] == 3260 and r["volume"] == 2210827100
    assert r["real_buy_volume"] == 1345256558 and r["legal_buy_count"] == 24
    assert r["bid_prices"] == [3350, 3340, 3330, 3320, 3310]
    assert r["ask_volumes"] == [500, 1000, 1500, 2000, 2500]
    assert r["price_limit_high"] == 3350 and r["data_quality"] == "ok"
    # instrument without client type but with volume -> partial, never invented
    assert rows[1]["real_buy_volume"] is None and rows[1]["data_quality"] == "partial"


def test_daily_rows_skip_no_trade_days_and_attach_adjusted_close():
    raw = pl.DataFrame([
        {"dEven": 20260922, "priceFirst": 3380.0, "priceMax": 3380.0, "priceMin": 3200.0, "pClosing": 3260.0,
         "pDrCotVal": 3230.0, "priceYesterday": 3290.0, "qTotTran5J": 3.7e9, "qTotCap": 1.2e13, "zTotTran": 45559},
        {"dEven": 20260921, "priceFirst": 0.0, "priceMax": 0.0, "priceMin": 0.0, "pClosing": 3290.0,
         "pDrCotVal": 3290.0, "priceYesterday": 3290.0, "qTotTran5J": 0.0, "qTotCap": 0.0, "zTotTran": 0},
    ])
    adj = pl.DataFrame({"date": [date(2026, 9, 22)], "pc": [3000]})
    rows = build_daily_price_rows("x", raw, adj, NOW)
    assert len(rows) == 1
    assert rows[0]["date"] == date(2026, 9, 22) and rows[0]["close"] == 3260.0 and rows[0]["adj_close"] == 3000.0


def test_client_type_mapping_individual_vs_institutional():
    hist = pl.DataFrame([{
        "recDate": 20260922, "buy_I_Volume": 10.0, "buy_N_Volume": 2.0, "buy_I_Value": 100.0, "buy_N_Value": 20.0,
        "buy_I_Count": 9.0, "buy_N_Count": 1, "sell_I_Volume": 11.0, "sell_N_Volume": 1.0, "sell_I_Value": 110.0,
        "sell_N_Value": 10.0, "sell_I_Count": 7, "sell_N_Count": 1,
    }])
    r = build_client_type_rows("x", hist, NOW)[0]
    assert r["real_buy_value"] == 100.0 and r["legal_buy_value"] == 20.0
    assert r["real_buy_count"] == 9 and r["real_sell_count"] == 7


def test_eod_rows_derive_values_from_vwap():
    sn = IntradaySnapshot(ins_code="x", ts=NOW, trade_date=date(2026, 9, 23), volume=100.0, value=1000.0,
                          open=10, high=11, low=9, close=10, last=10, yesterday=9.5, trade_count=5,
                          real_buy_volume=60.0, real_sell_volume=40.0, legal_buy_volume=40.0,
                          legal_sell_volume=60.0, real_buy_count=3, real_sell_count=2,
                          legal_buy_count=1, legal_sell_count=1)
    prices, cts = build_eod_rows_from_snapshots([sn], NOW)
    assert prices[0]["source"] == "tsetmc_snapshot"
    assert cts[0]["real_buy_value"] == 600.0  # 60 shares * VWAP 10
