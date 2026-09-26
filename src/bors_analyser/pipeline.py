"""Shared feature pipeline so backtests, training and live analysis compute features identically."""

from __future__ import annotations

from datetime import date

import polars as pl

from bors_analyser.backtest.labels import add_forward_labels
from bors_analyser.decision.scoring import market_features
from bors_analyser.features.data import load_daily
from bors_analyser.features.tablo import daily_tablo_panel
from bors_analyser.features.technical import technical_panel


def build_feature_panel(
    start: date | None = None, ins_codes: list[str] | None = None, min_avg_value: float = 5e9
) -> pl.DataFrame:
    """Daily tablo + technical + market-context features for every (instrument, day)."""
    daily = load_daily(ins_codes=None, start=start)  # market features need the whole universe
    panel = technical_panel(daily_tablo_panel(daily))
    panel = panel.join(market_features(panel, min_avg_value), on="date", how="left")
    if ins_codes:
        panel = panel.filter(pl.col("ins_code").is_in(ins_codes))
    return panel


def build_research_panel(start: date | None = None, min_avg_value: float = 5e9) -> pl.DataFrame:
    """Feature panel + forward outcome labels (for backtests and training only)."""
    return add_forward_labels(build_feature_panel(start, min_avg_value=min_avg_value))
