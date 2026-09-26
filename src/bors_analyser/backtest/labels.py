"""Forward-looking outcome labels. Used ONLY as targets/evaluation, never as features.

Convention: a signal observed at the close of day t is entered at the **open of day t+1**
(adjusted). Horizon h exits at the close of day t+h. Days that trade at a single price
(high == low) with a gap up are treated as locked limit-up queues where a buy fill is unrealistic.
"""

from __future__ import annotations

import polars as pl

HORIZONS = (1, 3, 5, 10)


def add_forward_labels(panel: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS) -> pl.DataFrame:
    df = panel.sort("ins_code", "date")
    f = pl.when(pl.col("close") > 0).then(pl.col("adj_close") / pl.col("close")).otherwise(1.0)
    df = df.with_columns(
        (pl.col("open") * f).alias("_a_open"), (pl.col("high") * f).alias("_a_high"),
        (pl.col("low") * f).alias("_a_low"),
    )

    def fwd(col: str, k: int) -> pl.Expr:
        return pl.col(col).shift(-k).over("ins_code")

    entry = fwd("_a_open", 1)
    exprs = [
        entry.alias("entry_price"),
        fwd("date", 1).alias("entry_date"),
        (
            (fwd("high", 1) == fwd("low", 1)) & (fwd("open", 1) > fwd("yesterday", 1))
        ).alias("entry_unfillable"),
    ]
    for h in horizons:
        exprs.append((fwd("adj_close", h) / entry - 1).alias(f"ret_{h}d"))
        exprs.append((pl.max_horizontal([fwd("_a_high", k) for k in range(1, h + 1)]) / entry - 1).alias(f"max_gain_{h}d"))
        exprs.append((pl.min_horizontal([fwd("_a_low", k) for k in range(1, h + 1)]) / entry - 1).alias(f"max_drawdown_{h}d"))
    df = df.with_columns(exprs).drop("_a_open", "_a_high", "_a_low")
    # Horizon only valid when the full window exists
    return df.with_columns(
        [pl.when(fwd("date", h).is_not_null()).then(pl.col(f"ret_{h}d")).otherwise(None).alias(f"ret_{h}d")
         for h in horizons]
    )


def target_hit(threshold: float, horizon: int) -> pl.Expr:
    """P-target: price reaches +threshold (intraday high) within `horizon` trading days."""
    col = f"max_gain_{horizon}d"
    return pl.when(pl.col(col).is_null()).then(None).otherwise(pl.col(col) >= threshold).cast(pl.Int8)


def target_return(threshold: float, horizon: int) -> pl.Expr:
    """P-target: close-to-entry return over `horizon` days exceeds threshold."""
    col = f"ret_{horizon}d"
    return pl.when(pl.col(col).is_null()).then(None).otherwise(pl.col(col) > threshold).cast(pl.Int8)
