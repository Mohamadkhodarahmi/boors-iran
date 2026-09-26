"""Intraday time-series behaviour from the session's snapshots.

For each metric we keep the path through the session and summarise it as:
- change over the last 5 / 15 / 30 minutes,
- a regime: `rising`, `falling`, `flat` or `jump` (sudden move).

Thresholds come from `config/scoring.yaml` (`timeseries:` section) with safe defaults.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from bors_analyser.features.tablo import orderbook_features

DEFAULTS = {"flat_rel_change": 0.03, "jump_sigma": 3.0, "min_points": 3}


def _parse_list(v: Any) -> list | None:
    if v is None or isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def snapshot_metrics(s: dict[str, Any]) -> dict[str, float | None]:
    """Metrics at one moment (all cumulative-for-the-day quantities)."""
    s = dict(s)
    for k in ("bid_prices", "bid_volumes", "bid_counts", "ask_prices", "ask_volumes", "ask_counts"):
        s[k] = _parse_list(s.get(k))
    rbv, rbc = s.get("real_buy_volume"), s.get("real_buy_count")
    rsv, rsc = s.get("real_sell_volume"), s.get("real_sell_count")
    buyer_power = None
    if rbv and rbc and rsv and rsc:
        buyer_power = (rbv / rbc) / (rsv / rsc)
    vwap = (s["value"] / s["volume"]) if s.get("value") and s.get("volume") else None
    real_flow = ((rbv or 0) - (rsv or 0)) * vwap if vwap is not None and rbv is not None else None
    ob = orderbook_features(s)
    return {
        "price": s.get("last"),
        "volume": s.get("volume"),
        "trade_count": float(s["trade_count"]) if s.get("trade_count") is not None else None,
        "buyer_power": buyer_power,
        "real_money_flow": real_flow,
        "bid_ask_imbalance": ob.get("bid_ask_value_imbalance"),
    }


def _value_at_or_before(points: list[tuple[datetime, float]], t: datetime) -> float | None:
    val = None
    for ts, v in points:
        if ts <= t:
            val = v
        else:
            break
    return val


def classify_regime(values: list[float], cfg: dict[str, float] | None = None) -> str:
    """rising / falling / flat / jump from a short series of metric values."""
    c = {**DEFAULTS, **(cfg or {})}
    vals = [v for v in values if v is not None and not math.isnan(v)]
    if len(vals) < c["min_points"]:
        return "insufficient_data"
    diffs = [b - a for a, b in zip(vals, vals[1:], strict=False)]
    if len(diffs) >= 3:
        hist = diffs[:-1]
        mean = sum(hist) / len(hist)
        sd = math.sqrt(sum((d - mean) ** 2 for d in hist) / len(hist))
        scale = max(sd, abs(mean), 1e-12)
        if abs(diffs[-1] - mean) > c["jump_sigma"] * scale and abs(diffs[-1]) > 0:
            return "jump"
    first, last = vals[0], vals[-1]
    base = max(abs(first), abs(last), 1e-12)
    rel = (last - first) / base
    if abs(rel) < c["flat_rel_change"]:
        return "flat"
    return "rising" if rel > 0 else "falling"


def intraday_behaviour(
    snapshots: list[dict[str, Any]], cfg: dict[str, float] | None = None, window_min: int = 30
) -> dict[str, Any]:
    """Summaries per metric from time-ordered snapshots of one instrument for one day."""
    snaps = sorted(snapshots, key=lambda s: s["ts"])
    if not snaps:
        return {}
    series: dict[str, list[tuple[datetime, float]]] = {}
    for s in snaps:
        for k, v in snapshot_metrics(s).items():
            if v is not None:
                series.setdefault(k, []).append((s["ts"], float(v)))

    now = snaps[-1]["ts"]
    out: dict[str, Any] = {"asof": now.isoformat(), "points": len(snaps)}
    for metric, pts in series.items():
        current = pts[-1][1]
        entry: dict[str, Any] = {"current": current}
        for m in (5, 15, 30):
            past = _value_at_or_before(pts, now - timedelta(minutes=m))
            entry[f"change_{m}m"] = (current - past) if past is not None else None
        recent = [v for ts, v in pts if ts >= now - timedelta(minutes=window_min)]
        entry["regime"] = classify_regime(recent, cfg)
        entry["path"] = [(ts.strftime("%H:%M"), round(v, 4)) for ts, v in pts[-12:]]
        out[metric] = entry
    return out
