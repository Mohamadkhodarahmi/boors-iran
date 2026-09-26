"""Assemble evidence -> scores -> narrative for one or many symbols, and persist `analyses` rows."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl
from sqlalchemy import select

from bors_analyser.analysis import divergence, narrator
from bors_analyser.backtest.engine import condition_study, conditions_expr, parse_conditions, save_run
from bors_analyser.config import get_scoring_config
from bors_analyser.db.models import Analysis, BacktestRun
from bors_analyser.db.session import session_scope, upsert
from bors_analyser.decision.model import active_models, model_outputs
from bors_analyser.decision.scoring import score_all
from bors_analyser.features.data import load_instruments, load_snapshots
from bors_analyser.features.fundamental import fundamental_features_for
from bors_analyser.features.tablo import intraday_relative_volume, orderbook_features, queue_strength
from bors_analyser.features.timeseries import intraday_behaviour
from bors_analyser.pipeline import build_feature_panel, build_research_panel
from bors_analyser.timeutil import normalize_fa, now_tehran

log = logging.getLogger(__name__)

TABLO_KEYS = ["volume_ratio_5", "volume_ratio_20", "volume_ratio_30", "real_money_flow", "real_money_flow_ratio",
              "real_money_flow_ratio_5", "cum_real_money_flow_5", "cum_real_money_flow_20", "inflow_days_5",
              "buyer_power", "buyer_power_prev5", "buyer_power_vs_prev5", "avg_real_buyer_value",
              "avg_real_seller_value", "trade_count_ratio_20", "avg_trade_value_ratio_20", "close_return",
              "last_return", "last_vs_close", "gap", "dist_from_high", "dist_from_low", "momentum_5",
              "momentum_20", "avg_value_20", "value", "volume", "close", "last"]
TECH_KEYS = ["trend", "rsi_14", "macd_hist_pct", "sma_20", "sma_50", "sma_200", "support_20", "resistance_20",
             "support_60", "resistance_60", "dist_to_resistance_20", "dist_to_support_20", "breakout_20",
             "breakout_60", "breakdown_20", "volatility_20", "volatility_ratio", "atr_pct", "bb_position",
             "dist_sma_50", "sma_50_slope_10"]
MARKET_KEYS = ["market_breadth", "market_median_return", "market_money_flow_ratio", "market_real_money_flow"]


def _clean(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


# --------------------------------------------------------------------------- setups (history)


def run_setup_studies(start: date = date(2018, 1, 1), eval_start: date = date(2019, 1, 1)) -> dict[str, str]:
    cfg = get_scoring_config()
    panel = build_research_panel(start=start, min_avg_value=cfg["universe"]["min_avg_value_20"])
    out = {}
    for sid, s in (cfg.get("setups") or {}).items():
        res = condition_study(panel, parse_conditions(s["conditions"]), start=eval_start,
                              min_avg_value=cfg["universe"]["min_avg_value_20"])
        out[sid] = save_run(f"setup:{sid}", res)
        sig = res["signal"].get("return_after_5d", {})
        log.info("setup %s: n=%s mean5d=%.4f win5d=%.3f", sid, sig.get("n"), sig.get("mean", 0), sig.get("win_rate", 0))
    return out


def _latest_setup_results() -> dict[str, dict[str, Any]]:
    cfg = get_scoring_config()
    out = {}
    with session_scope() as s:
        for sid in cfg.get("setups") or {}:
            run = s.execute(select(BacktestRun).where(BacktestRun.name == f"setup:{sid}")
                            .order_by(BacktestRun.created_at.desc()).limit(1)).scalars().first()
            if run:
                out[sid] = run.results
    return out


def matching_setups(row: dict[str, Any], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cfg = get_scoring_config()
    matched = []
    frame = pl.DataFrame([{k: v for k, v in row.items() if not isinstance(v, (list, dict))}], strict=False)
    for sid, s in (cfg.get("setups") or {}).items():
        conds = parse_conditions(s["conditions"])
        if any(c not in frame.columns for c, _, _ in conds):
            continue
        if frame.filter(conditions_expr(conds)).height and sid in results:
            r = results[sid]
            matched.append({
                "id": sid, "fa": s["fa"], "period": r.get("period") or ["", ""],
                "return_after_1d": r["signal"].get("return_after_1d"),
                "return_after_5d": r["signal"].get("return_after_5d"),
                "return_after_10d": r["signal"].get("return_after_10d"),
                "baseline_5d": r["baseline"].get("return_after_5d"),
            })
    return matched


# --------------------------------------------------------------------------- live session overlay


def live_overlay(snaps: list[dict[str, Any]], daily_row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Today's tablo metrics from intraday snapshots (daily row = previous close baselines)."""
    last = snaps[-1]
    ob = orderbook_features(last)
    avg_value = daily_row.get("avg_value_20")
    ob["queue_strength"] = queue_strength(ob.get("buy_queue_value") or ob.get("sell_queue_value"), avg_value)
    behaviour = intraday_behaviour(snaps, get_scoring_config().get("timeseries"))
    t: dict[str, Any] = {}
    vol, val = last.get("volume"), last.get("value")
    prev_avg_vol = (daily_row.get("volume") / daily_row["volume_ratio_20"]
                    if daily_row.get("volume") and daily_row.get("volume_ratio_20") else None)
    if vol and prev_avg_vol:
        t["volume_ratio_20"] = vol / prev_avg_vol
        t["relative_volume"] = intraday_relative_volume(vol, prev_avg_vol, last["ts"])
    bp_now = (behaviour.get("buyer_power") or {}).get("current")
    if bp_now is not None:
        t["buyer_power"] = bp_now
    flow_now = (behaviour.get("real_money_flow") or {}).get("current")
    if flow_now is not None:
        t["real_money_flow"] = flow_now
        if val:
            t["real_money_flow_ratio"] = flow_now / val
    if last.get("last") and last.get("yesterday"):
        t["last_return"] = last["last"] / last["yesterday"] - 1
    if last.get("close") and last.get("yesterday"):
        t["close_return"] = last["close"] / last["yesterday"] - 1
    if last.get("last") and last.get("high"):
        t["dist_from_high"] = last["last"] / last["high"] - 1
    t["last"], t["close"], t["value"], t["volume"] = last.get("last"), last.get("close"), val, vol
    return t, ob, behaviour


# --------------------------------------------------------------------------- main entry


def analyze(symbols: list[str] | None = None, modes: tuple[str, ...] = ("short", "swing", "fundamental"),
            asof: datetime | None = None, persist: bool = True, with_fundamentals: bool = True) -> list[dict[str, Any]]:
    cfg = get_scoring_config()
    asof = asof or now_tehran().replace(tzinfo=None)
    inst = load_instruments()
    if symbols:
        wanted = {normalize_fa(s) for s in symbols}
        inst = inst.filter(pl.col("symbol").is_in(list(wanted)))
    if inst.is_empty():
        return []
    codes = inst["ins_code"].to_list()
    panel = build_feature_panel(start=asof.date() - timedelta(days=500),
                                min_avg_value=cfg["universe"]["min_avg_value_20"])
    latest = panel.filter(pl.col("ins_code").is_in(codes)).sort("date").group_by("ins_code").tail(1)
    if latest.is_empty():
        return []

    models = active_models()
    preds = model_outputs(models, latest) if models else {}
    setup_results = _latest_setup_results()

    snaps_df = load_snapshots(asof.date(), codes)
    snaps_by_code: dict[str, list[dict[str, Any]]] = {}
    if not snaps_df.is_empty():
        for r in snaps_df.iter_rows(named=True):
            snaps_by_code.setdefault(r["ins_code"], []).append(r)

    info = {r["ins_code"]: r for r in inst.iter_rows(named=True)}
    results = []
    rows_to_store = []
    for row in latest.iter_rows(named=True):
        code = row["ins_code"]
        meta = info[code]
        tablo = {k: row.get(k) for k in TABLO_KEYS}
        technical = {k: row.get(k) for k in TECH_KEYS}
        market = {k: row.get(k) for k in MARKET_KEYS}
        data_time = f"{row['date']} (پایانی)"
        orderbook, intraday = {}, {}
        snaps = snaps_by_code.get(code) or []
        if snaps and snaps[-1]["trade_date"] > row["date"]:
            live_t, orderbook, intraday = live_overlay(snaps, row)
            tablo.update({k: v for k, v in live_t.items() if v is not None})
            data_time = snaps[-1]["ts"].strftime("%Y-%m-%d %H:%M:%S")
        elif snaps:
            orderbook = orderbook_features(snaps[-1])
            orderbook["queue_strength"] = queue_strength(
                orderbook.get("buy_queue_value") or orderbook.get("sell_queue_value"), row.get("avg_value_20"))
            intraday = intraday_behaviour(snaps, cfg.get("timeseries"))

        price = tablo.get("close") or row.get("close")
        mcap = price * meta["total_shares"] if price and meta.get("total_shares") else None
        fundamental = (fundamental_features_for(meta["symbol"], asof, mcap, cfg.get("fundamental"))
                       if with_fundamentals else {})
        features = {**row, **tablo, **technical, **market,
                    **{k: v for k, v in fundamental.items() if k not in ("recent_titles", "data_quality")}}
        setups = matching_setups(features, setup_results)

        for mode in modes:
            scores = score_all(features, cfg, mode)
            ev = _clean({
                "symbol": meta["symbol"], "name": meta["name"], "sector": meta["sector_name"],
                "market": meta["market"], "ins_code": code, "mode": mode, "asof": asof,
                "data_time": data_time, "tablo": tablo, "orderbook": orderbook, "intraday": intraday,
                "technical": technical, "market_context": market, "fundamental": fundamental,
                "scores": scores, "divergence": divergence.detect(scores["directions"]),
                "model": preds.get(code, {}), "setups": setups,
            })
            report = narrator.build_report(ev)
            text = narrator.telegram_text(ev, report, mode)
            ev["report"] = report
            ev["telegram_text"] = text
            results.append(ev)
            rows_to_store.append({
                "ins_code": code, "asof": asof, "mode": mode, "symbol": meta["symbol"],
                "label": scores["label"], "scores": _clean({k: v for k, v in scores.items() if k != "components"}),
                "probability": _clean({k: v for k, v in (preds.get(code) or {}).items() if v.get("calibrated")})
                or None,
                "evidence": ev, "narrative": report["narrative"], "risks": report["sections"]["risks"],
                "scan_tags": None, "data_time": None, "published_at": None,
            })
    if persist and rows_to_store:
        with session_scope() as s:
            upsert(s, Analysis, rows_to_store)
    log.info("analysis: %d symbols x %d modes", latest.height, len(modes))
    return results
