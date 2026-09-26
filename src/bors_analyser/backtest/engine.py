"""Condition studies: "when these conditions held in the past, what happened next?"

Results are always reported next to the unconditional baseline of the same universe and period,
because in a high-inflation market a positive average return means little by itself.
"""

from __future__ import annotations

import logging
import operator
import re
import uuid
from datetime import date
from typing import Any

import polars as pl

from bors_analyser.backtest.labels import HORIZONS
from bors_analyser.db.models import BacktestRun
from bors_analyser.db.session import session_scope
from bors_analyser.timeutil import now_tehran

log = logging.getLogger(__name__)

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne}
_COND_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")


def parse_conditions(text: str) -> list[tuple[str, str, Any]]:
    """'volume_ratio_20>3, buyer_power>=2, trend==up' -> [(col, op, value)]."""
    out = []
    for part in [p for p in text.split(",") if p.strip()]:
        m = _COND_RE.match(part)
        if not m:
            raise ValueError(f"cannot parse condition: {part!r}")
        col, op, raw = m.groups()
        val: Any
        if raw.lower() in ("true", "false"):
            val = raw.lower() == "true"
        else:
            try:
                val = float(raw)
            except ValueError:
                val = raw.strip("'\"")
        out.append((col, op, val))
    return out


def conditions_expr(conds: list[tuple[str, str, Any]]) -> pl.Expr:
    expr = pl.lit(True)
    for col, op, val in conds:
        expr = expr & _OPS[op](pl.col(col), pl.lit(val)).fill_null(False)
    return expr


def _stats(df: pl.DataFrame, horizons: tuple[int, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"sample_count": df.height}
    if df.is_empty():
        return out
    for h in horizons:
        r = df[f"ret_{h}d"].drop_nulls()
        if r.is_empty():
            continue
        out[f"return_after_{h}d"] = {
            "n": r.len(),
            "mean": float(r.mean()),
            "median": float(r.median()),
            "win_rate": float((r > 0).mean()),
            "max_gain": float(df[f"max_gain_{h}d"].max()),
            "max_loss": float(r.min()),
            "avg_max_drawdown": float(df[f"max_drawdown_{h}d"].mean()),
            "worst_max_drawdown": float(df[f"max_drawdown_{h}d"].min()),
        }
    return out


def _dedupe(df: pl.DataFrame, cooldown: int) -> pl.DataFrame:
    """Keep the first signal per instrument, then ignore repeats within `cooldown` rows (trading days)."""
    if cooldown <= 0 or df.is_empty():
        return df
    df = df.sort("ins_code", "date").with_row_index("_i")
    keep: list[int] = []
    for _, g in df.group_by("ins_code", maintain_order=True):
        last_idx = None
        for i, ridx in zip(g["_i"], g["_row_in_instrument"], strict=False):
            if last_idx is None or ridx - last_idx > cooldown:
                keep.append(i)
                last_idx = ridx
    return df.filter(pl.col("_i").is_in(keep)).drop("_i")


def condition_study(
    panel: pl.DataFrame,
    conditions: list[tuple[str, str, Any]],
    *,
    horizons: tuple[int, ...] = HORIZONS,
    min_avg_value: float = 5e9,
    start: date | None = None,
    end: date | None = None,
    cooldown: int = 5,
) -> dict[str, Any]:
    df = panel.with_columns(
        pl.int_range(pl.len()).over("ins_code", order_by="date").alias("_row_in_instrument")
    )
    universe = df.filter(
        pl.col("entry_price").is_not_null()
        & ~pl.col("entry_unfillable").fill_null(False)
        & (pl.col("avg_value_20") >= min_avg_value)
    )
    if start:
        universe = universe.filter(pl.col("date") >= start)
    if end:
        universe = universe.filter(pl.col("date") <= end)
    hits = _dedupe(universe.filter(conditions_expr(conditions)), cooldown)

    signal = _stats(hits, horizons)
    baseline = _stats(universe, horizons)
    for h in horizons:
        k = f"return_after_{h}d"
        if k in signal and k in baseline:
            signal[k]["excess_mean_vs_baseline"] = signal[k]["mean"] - baseline[k]["mean"]
            signal[k]["excess_win_rate_vs_baseline"] = signal[k]["win_rate"] - baseline[k]["win_rate"]
    by_year = {}
    if not hits.is_empty():
        for (yr,), g in hits.with_columns(pl.col("date").dt.year().alias("_y")).group_by("_y", maintain_order=True):
            r = g["ret_5d"].drop_nulls()
            if r.len():
                by_year[int(yr)] = {"n": r.len(), "mean_5d": float(r.mean()), "win_rate_5d": float((r > 0).mean())}
    return {
        "conditions": [f"{c}{o}{v}" for c, o, v in conditions],
        "period": [str(universe["date"].min()), str(universe["date"].max())] if universe.height else None,
        "universe_filter": {"min_avg_value_20": min_avg_value, "cooldown_days": cooldown,
                            "entry": "next day open (adjusted)", "excluded": "locked limit-up entries"},
        "signal": signal,
        "baseline": baseline,
        "by_year": dict(sorted(by_year.items())),
        "recent_examples": hits.sort("date", descending=True).head(10)
        .select("ins_code", "date", "ret_5d").to_dicts() if hits.height else [],
    }


def save_run(name: str, result: dict[str, Any]) -> str:
    run_id = uuid.uuid4().hex[:12]
    period = result.get("period") or [None, None]
    with session_scope() as s:
        s.add(BacktestRun(
            run_id=run_id, created_at=now_tehran().replace(tzinfo=None), name=name,
            conditions={"conditions": result["conditions"], **result["universe_filter"]},
            period_start=date.fromisoformat(period[0]) if period[0] else None,
            period_end=date.fromisoformat(period[1]) if period[1] else None,
            results={k: v for k, v in result.items() if k != "recent_examples"} | {
                "recent_examples": [{**e, "date": str(e["date"])} for e in result["recent_examples"]]},
        ))
    return run_id
