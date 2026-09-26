"""Tablo-reading features (volume, money flow, buyer power, order book, price and trade behaviour).

Two entry points:
- `daily_tablo_panel(daily)` — vectorised over all instruments and all days. Every baseline
  (averages, z-scores) is computed from *previous* days only (`shift(1)`), so the row for day t
  uses information available at the close of day t and nothing later. The same function feeds
  live analysis and the backtester.
- `orderbook_features(snapshot)` / `intraday_relative_volume(...)` — for the live session.

Money is in Rial. Missing inputs give null features, never zeros.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from typing import Any

import polars as pl

SESSION_OPEN = time(9, 0)
SESSION_CLOSE = time(12, 30)
SESSION_MINUTES = 210


def _safe_div(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.when((b != 0) & b.is_not_null()).then(a / b).otherwise(None)


def _prev_mean(col: str, n: int) -> pl.Expr:
    """Mean of the previous n rows (excludes the current row) within each instrument."""
    return pl.col(col).shift(1).rolling_mean(window_size=n, min_samples=max(2, n // 2)).over("ins_code")


def daily_tablo_panel(daily: pl.DataFrame) -> pl.DataFrame:
    """`daily` must be sorted by (ins_code, date) and contain price + client-type columns."""
    df = daily.sort("ins_code", "date").with_columns(
        # non-positive prices are feed errors -> missing
        [pl.when(pl.col(c) > 0).then(pl.col(c)).otherwise(None).alias(c)
         for c in ("open", "high", "low", "close", "last", "yesterday", "adj_close") if c in daily.columns]
    )

    # Adjusted close: official adj_close where present, otherwise close × latest known factor.
    df = df.with_columns(
        _safe_div(pl.col("adj_close"), pl.col("close")).alias("_adj_factor")
    ).with_columns(
        pl.col("_adj_factor").forward_fill().over("ins_code").fill_null(1.0).alias("_adj_factor")
    ).with_columns(
        pl.coalesce(pl.col("adj_close"), pl.col("close") * pl.col("_adj_factor")).alias("adj_close")
    )

    df = df.with_columns(
        # ---- money flow & buyer power
        (pl.col("real_buy_value") - pl.col("real_sell_value")).alias("real_money_flow"),
        _safe_div(pl.col("real_buy_value"), pl.col("real_buy_count")).alias("avg_real_buyer_value"),
        _safe_div(pl.col("real_sell_value"), pl.col("real_sell_count")).alias("avg_real_seller_value"),
        _safe_div(pl.col("value"), pl.col("trade_count")).alias("avg_trade_value"),
        pl.col("volume").log1p().alias("_log_vol"),
    ).with_columns(
        _safe_div(pl.col("avg_real_buyer_value"), pl.col("avg_real_seller_value")).alias("buyer_power"),
        _safe_div(pl.col("real_money_flow"), pl.col("value")).alias("real_money_flow_ratio"),
    )

    df = df.with_columns(
        # ---- volume
        _safe_div(pl.col("volume"), _prev_mean("volume", 5)).alias("volume_ratio_5"),
        _safe_div(pl.col("volume"), _prev_mean("volume", 20)).alias("volume_ratio_20"),
        _safe_div(pl.col("volume"), _prev_mean("volume", 30)).alias("volume_ratio_30"),
        _safe_div(pl.col("value"), _prev_mean("value", 20)).alias("value_ratio_20"),
        _prev_mean("value", 20).alias("avg_value_20"),
        # volume z-score on log scale vs previous 60 days
        _safe_div(
            pl.col("_log_vol") - _prev_mean("_log_vol", 60),
            pl.col("_log_vol").shift(1).rolling_std(window_size=60, min_samples=20).over("ins_code"),
        ).alias("volume_zscore_60"),
        # ---- money flow history
        pl.col("real_money_flow").rolling_sum(window_size=5, min_samples=3).over("ins_code")
        .alias("cum_real_money_flow_5"),
        pl.col("real_money_flow").rolling_sum(window_size=20, min_samples=10).over("ins_code")
        .alias("cum_real_money_flow_20"),
        _safe_div(
            pl.col("real_money_flow").rolling_sum(window_size=5, min_samples=3).over("ins_code"),
            pl.col("value").rolling_sum(window_size=5, min_samples=3).over("ins_code"),
        ).alias("real_money_flow_ratio_5"),
        (pl.col("real_money_flow") > 0).cast(pl.Int8).rolling_sum(window_size=5, min_samples=3)
        .over("ins_code").alias("inflow_days_5"),
        # ---- buyer power history
        _prev_mean("buyer_power", 5).alias("buyer_power_prev5"),
        _prev_mean("buyer_power", 20).alias("buyer_power_prev20"),
        # ---- trade behaviour
        _safe_div(pl.col("trade_count"), _prev_mean("trade_count", 20)).alias("trade_count_ratio_20"),
        _safe_div(pl.col("avg_trade_value"), _prev_mean("avg_trade_value", 20)).alias("avg_trade_value_ratio_20"),
        # ---- price behaviour
        _safe_div(pl.col("close") - pl.col("yesterday"), pl.col("yesterday")).alias("close_return"),
        _safe_div(pl.col("last") - pl.col("yesterday"), pl.col("yesterday")).alias("last_return"),
        _safe_div(pl.col("last") - pl.col("close"), pl.col("close")).alias("last_vs_close"),
        _safe_div(pl.col("open") - pl.col("yesterday"), pl.col("yesterday")).alias("gap"),
        _safe_div(pl.col("last") - pl.col("high"), pl.col("high")).alias("dist_from_high"),
        _safe_div(pl.col("last") - pl.col("low"), pl.col("low")).alias("dist_from_low"),
        _safe_div(pl.col("adj_close"), pl.col("adj_close").shift(5).over("ins_code")).sub(1).alias("momentum_5"),
        _safe_div(pl.col("adj_close"), pl.col("adj_close").shift(20).over("ins_code")).sub(1).alias("momentum_20"),
        _safe_div(pl.col("adj_close"), pl.col("adj_close").shift(60).over("ins_code")).sub(1).alias("momentum_60"),
    ).with_columns(
        _safe_div(pl.col("buyer_power"), pl.col("buyer_power_prev5")).alias("buyer_power_vs_prev5"),
        (pl.col("buyer_power") - pl.col("buyer_power").shift(1).over("ins_code")).alias("buyer_power_change_1d"),
    )
    return df.drop("_log_vol", "_adj_factor")


TABLO_FEATURES = [
    "volume_ratio_5", "volume_ratio_20", "volume_ratio_30", "value_ratio_20", "volume_zscore_60",
    "real_money_flow", "real_money_flow_ratio", "cum_real_money_flow_5", "cum_real_money_flow_20",
    "real_money_flow_ratio_5", "inflow_days_5",
    "avg_real_buyer_value", "avg_real_seller_value", "buyer_power", "buyer_power_prev5",
    "buyer_power_vs_prev5", "buyer_power_change_1d",
    "trade_count_ratio_20", "avg_trade_value", "avg_trade_value_ratio_20",
    "close_return", "last_return", "last_vs_close", "gap", "dist_from_high", "dist_from_low",
    "momentum_5", "momentum_20", "momentum_60",
]


# --------------------------------------------------------------------------- live session


def session_elapsed_fraction(ts: datetime) -> float:
    minutes = (ts.hour * 60 + ts.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
    return min(1.0, max(minutes, 1) / SESSION_MINUTES)


def intraday_relative_volume(volume_so_far: float | None, avg_daily_volume: float | None, ts: datetime) -> float | None:
    """Volume so far vs. the volume expected by this time of the session (linear pacing)."""
    if not volume_so_far or not avg_daily_volume:
        return None
    return volume_so_far / (avg_daily_volume * session_elapsed_fraction(ts))


def _num(xs: Sequence[Any] | None) -> list[float]:
    return [float(x) if x is not None else 0.0 for x in (xs or [])]


def orderbook_features(snap: dict[str, Any]) -> dict[str, float | bool | None]:
    """Features from one 5-level order-book snapshot (bid = demand, ask = supply)."""
    bp, bv, bc = _num(snap.get("bid_prices")), _num(snap.get("bid_volumes")), _num(snap.get("bid_counts"))
    ap, av, ac = _num(snap.get("ask_prices")), _num(snap.get("ask_volumes")), _num(snap.get("ask_counts"))
    out: dict[str, float | bool | None] = {}
    if not bp and not ap:
        return out

    def imb(a: float, b: float) -> float | None:
        return (a - b) / (a + b) if (a + b) > 0 else None

    bid_vol, ask_vol = sum(bv), sum(av)
    bid_val = sum(p * v for p, v in zip(bp, bv, strict=False))
    ask_val = sum(p * v for p, v in zip(ap, av, strict=False))
    out["bid_ask_volume_imbalance"] = imb(bid_vol, ask_vol)
    out["bid_ask_value_imbalance"] = imb(bid_val, ask_val)
    out["level1_imbalance"] = imb(bv[0] if bv else 0.0, av[0] if av else 0.0)
    out["order_count_imbalance"] = imb(sum(bc), sum(ac))
    out["bid_concentration"] = (bv[0] / bid_vol) if bid_vol else None
    out["ask_concentration"] = (av[0] / ask_vol) if ask_vol else None
    best_bid = bp[0] if bp and bv and bv[0] > 0 else None
    best_ask = ap[0] if ap and av and av[0] > 0 else None
    if best_bid and best_ask:
        mid = (best_bid + best_ask) / 2
        out["spread"] = (best_ask - best_bid) / mid
    else:
        out["spread"] = None

    hi, lo = snap.get("price_limit_high"), snap.get("price_limit_low")
    # Buy queue: best bid sits at the upper price limit and there is no supply.
    buy_queue = bool(hi and best_bid and best_bid >= hi and not best_ask)
    sell_queue = bool(lo and best_ask and best_ask <= lo and not best_bid)
    out["buy_queue"] = buy_queue
    out["sell_queue"] = sell_queue
    out["buy_queue_value"] = bid_val if buy_queue else None
    out["sell_queue_value"] = ask_val if sell_queue else None
    return out


def queue_strength(queue_value: float | None, avg_value_20: float | None) -> float | None:
    """Queue size relative to a normal day's traded value (1.0 = one average day)."""
    if not queue_value or not avg_value_20:
        return None
    return queue_value / avg_value_20
