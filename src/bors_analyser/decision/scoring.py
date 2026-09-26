"""Rule-based component scores (0-100) driven entirely by `config/scoring.yaml`.

These are *descriptive* scores — shown as "Model Score", never as probabilities. Each component
reports `coverage` (share of its configured weight whose inputs were available), so a score built
from half the data is visibly weaker than one built from all of it.
"""

from __future__ import annotations

import math
import operator
from typing import Any

import polars as pl

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}


def market_features(panel: pl.DataFrame, min_avg_value: float = 5e9) -> pl.DataFrame:
    """Per-date market context from liquid stocks: breadth, median return, aggregate real flow."""
    liquid = panel.filter(pl.col("avg_value_20") >= min_avg_value)
    return liquid.group_by("date").agg(
        (pl.col("close_return") > 0).mean().alias("market_breadth"),
        pl.col("close_return").median().alias("market_median_return"),
        (pl.col("real_money_flow").sum() / pl.col("value").sum()).alias("market_money_flow_ratio"),
        pl.col("real_money_flow").sum().alias("market_real_money_flow"),
        pl.len().alias("market_liquid_count"),
    ).sort("date")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def sub_score(x: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * 100


def component_score(features: dict[str, Any], spec: dict[str, dict[str, float]]) -> dict[str, Any]:
    total_w = sum(s["weight"] for s in spec.values())
    acc, used_w, parts = 0.0, 0.0, {}
    for name, s in spec.items():
        x = _num(features.get(name))
        if x is None:
            continue
        sc = sub_score(x, s["lo"], s["hi"])
        acc += sc * s["weight"]
        used_w += s["weight"]
        parts[name] = round(sc, 1)
    if used_w == 0:
        return {"score": None, "coverage": 0.0, "parts": {}}
    return {"score": round(acc / used_w, 1), "coverage": round(used_w / total_w, 2), "parts": parts}


def risk_score(features: dict[str, Any], spec: dict[str, dict[str, Any]]) -> dict[str, Any]:
    points, flags = 0, []
    for name, r in spec.items():
        v = features.get(r["feature"])
        if v is None:
            continue
        target = r["value"]
        try:
            hit = _OPS[r["op"]](v, target) if isinstance(target, bool) else _OPS[r["op"]](float(v), float(target))
        except (TypeError, ValueError):
            continue
        if hit:
            points += r["points"]
            flags.append(name)
    return {"score": min(100, points), "flags": flags}


def label_for(score: float | None, labels: list[dict[str, Any]]) -> dict[str, str]:
    if score is None:
        return {"label": "Insufficient data", "fa": "داده ناکافی"}
    for lab in labels:
        if score >= lab["min"]:
            return {"label": lab["label"], "fa": lab["fa"]}
    return {"label": labels[-1]["label"], "fa": labels[-1]["fa"]}


def direction(score: float | None, thresholds: dict[str, float]) -> str | None:
    if score is None:
        return None
    if score >= thresholds["positive"]:
        return "+"
    if score <= thresholds["negative"]:
        return "-"
    return "0"


def score_all(features: dict[str, Any], cfg: dict[str, Any], mode: str = "swing") -> dict[str, Any]:
    comps = {name: component_score(features, spec) for name, spec in cfg["components"].items()}
    risk = risk_score(features, cfg["risk"])
    mode_cfg = cfg["modes"][mode]
    num, den = 0.0, 0.0
    for comp, w in mode_cfg["weights"].items():
        c = comps.get(comp, {})
        if c.get("score") is not None:
            # weight scaled by coverage: thin evidence counts less
            eff = w * max(c["coverage"], 0.0)
            num += c["score"] * eff
            den += eff
    base = num / den if den else None
    # Risk only subtracts (above a tolerance of 30); low risk is not a reason to score higher.
    penalty = mode_cfg["risk_penalty"] * max(0, risk["score"] - 30)
    overall = None if base is None else max(0.0, min(100.0, base - penalty))
    thresholds = cfg["direction_thresholds"]
    return {
        "mode": mode,
        "tablo_score": comps.get("tablo", {}).get("score"),
        "technical_score": comps.get("technical", {}).get("score"),
        "fundamental_score": comps.get("fundamental", {}).get("score"),
        "market_score": comps.get("market", {}).get("score"),
        "risk_score": risk["score"],
        "overall_score": None if overall is None else round(overall, 1),
        "components": comps,
        "risk_flags": risk["flags"],
        "directions": {k: direction(v.get("score"), thresholds) for k, v in comps.items()},
        **label_for(overall, cfg["labels"]),
        "score_kind": "model_score",  # never a probability
    }
