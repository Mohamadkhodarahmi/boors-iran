"""Technical indicators on adjusted prices, vectorised over all instruments.

OHLC are adjusted with the same factor as the close (adj_close / close), so capital increases and
dividends do not appear as crashes. Support/resistance use only *previous* days, so a breakout on
day t compares the close of t against levels known before t.

Technical analysis is one input among several; it never decides on its own.
"""

from __future__ import annotations

import polars as pl


def _over(e: pl.Expr) -> pl.Expr:
    return e.over("ins_code")


def technical_panel(daily: pl.DataFrame, sr_window: int = 20, sr_window_long: int = 60) -> pl.DataFrame:
    """`daily` needs ins_code, date, open, high, low, close, adj_close (from `daily_tablo_panel`)."""
    df = daily.sort("ins_code", "date").with_columns(
        pl.when(pl.col("close") > 0).then(pl.col("adj_close") / pl.col("close")).otherwise(1.0).alias("_f")
    ).with_columns(
        (pl.col("open") * pl.col("_f")).alias("a_open"),
        (pl.col("high") * pl.col("_f")).alias("a_high"),
        (pl.col("low") * pl.col("_f")).alias("a_low"),
        pl.col("adj_close").alias("a_close"),
    )
    c = pl.col("a_close")
    prev_c = _over(c.shift(1))
    df = df.with_columns(
        _over(c.rolling_mean(20, min_samples=20)).alias("sma_20"),
        _over(c.rolling_mean(50, min_samples=50)).alias("sma_50"),
        _over(c.rolling_mean(200, min_samples=150)).alias("sma_200"),
        _over(c.ewm_mean(span=12, adjust=False)).alias("ema_12"),
        _over(c.ewm_mean(span=26, adjust=False)).alias("ema_26"),
        _over(c.rolling_std(20, min_samples=20)).alias("_std20"),
        (c - prev_c).alias("_chg"),
        pl.max_horizontal(
            pl.col("a_high") - pl.col("a_low"),
            (pl.col("a_high") - prev_c).abs(),
            (pl.col("a_low") - prev_c).abs(),
        ).alias("_tr"),
        _over((c / c.shift(1)).log()).alias("_logret"),
        # levels known before today
        _over(pl.col("a_high").shift(1).rolling_max(sr_window, min_samples=sr_window // 2)).alias("resistance_20"),
        _over(pl.col("a_low").shift(1).rolling_min(sr_window, min_samples=sr_window // 2)).alias("support_20"),
        _over(pl.col("a_high").shift(1).rolling_max(sr_window_long, min_samples=sr_window_long // 2)).alias("resistance_60"),
        _over(pl.col("a_low").shift(1).rolling_min(sr_window_long, min_samples=sr_window_long // 2)).alias("support_60"),
    ).with_columns(
        (pl.col("ema_12") - pl.col("ema_26")).alias("macd"),
        _over(pl.col("_chg").clip(lower_bound=0).ewm_mean(alpha=1 / 14, adjust=False, min_samples=14)).alias("_gain"),
        _over((-pl.col("_chg")).clip(lower_bound=0).ewm_mean(alpha=1 / 14, adjust=False, min_samples=14)).alias("_loss"),
        _over(pl.col("_tr").ewm_mean(alpha=1 / 14, adjust=False, min_samples=14)).alias("atr_14"),
        (pl.col("sma_20") + 2 * pl.col("_std20")).alias("bb_upper"),
        (pl.col("sma_20") - 2 * pl.col("_std20")).alias("bb_lower"),
        (_over(pl.col("_logret").rolling_std(20, min_samples=15)) * (240 ** 0.5)).alias("volatility_20"),
        (_over(pl.col("_logret").rolling_std(60, min_samples=40)) * (240 ** 0.5)).alias("volatility_60"),
    ).with_columns(
        _over(pl.col("macd").ewm_mean(span=9, adjust=False)).alias("macd_signal"),
        pl.when(pl.col("_loss") == 0).then(100.0)
        .otherwise(100 - 100 / (1 + pl.col("_gain") / pl.col("_loss"))).alias("rsi_14"),
        ((c - pl.col("bb_lower")) / (pl.col("bb_upper") - pl.col("bb_lower"))).alias("bb_position"),
        (pl.col("atr_14") / c).alias("atr_pct"),
        (c / pl.col("sma_20") - 1).alias("dist_sma_20"),
        (c / pl.col("sma_50") - 1).alias("dist_sma_50"),
        (c / pl.col("sma_200") - 1).alias("dist_sma_200"),
        (pl.col("resistance_20") / c - 1).alias("dist_to_resistance_20"),
        (c / pl.col("support_20") - 1).alias("dist_to_support_20"),
        (pl.col("resistance_60") / c - 1).alias("dist_to_resistance_60"),
        (c > pl.col("resistance_20")).alias("breakout_20"),
        (c > pl.col("resistance_60")).alias("breakout_60"),
        (c < pl.col("support_20")).alias("breakdown_20"),
        (_over(pl.col("sma_50") / pl.col("sma_50").shift(10)) - 1).alias("sma_50_slope_10"),
    ).with_columns(
        (pl.col("macd") - pl.col("macd_signal")).alias("macd_hist"),
        ((pl.col("macd") - pl.col("macd_signal")) / c).alias("macd_hist_pct"),
        pl.when((c > pl.col("sma_50")) & (pl.col("sma_50") > pl.col("sma_200")) & (pl.col("sma_50_slope_10") > 0))
        .then(pl.lit("up"))
        .when((c < pl.col("sma_50")) & (pl.col("sma_50") < pl.col("sma_200")) & (pl.col("sma_50_slope_10") < 0))
        .then(pl.lit("down"))
        .when(pl.col("sma_200").is_null())
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.lit("sideways"))
        .alias("trend"),
        (pl.col("volatility_20") / pl.col("volatility_60")).alias("volatility_ratio"),
    )
    return df.drop([col for col in df.columns if col.startswith("_")] + ["a_open", "a_high", "a_low", "a_close"])


TECHNICAL_FEATURES = [
    "sma_20", "sma_50", "sma_200", "ema_12", "ema_26", "rsi_14", "macd", "macd_signal", "macd_hist", "macd_hist_pct",
    "atr_14", "atr_pct", "bb_upper", "bb_lower", "bb_position", "volatility_20", "volatility_60",
    "volatility_ratio", "support_20", "resistance_20", "support_60", "resistance_60",
    "dist_sma_20", "dist_sma_50", "dist_sma_200", "dist_to_resistance_20", "dist_to_support_20",
    "dist_to_resistance_60", "breakout_20", "breakout_60", "breakdown_20", "sma_50_slope_10", "trend",
]
