"""Statistical decision model: LightGBM per target + isotonic calibration + honest evaluation.

Pipeline (all splits are by time, never random):

    |---------------- train ----------------|purge|--- calibration (12m) ---|purge|--- test (12m) ---|
                         last 10% of train = early-stopping validation

- Features are computed at the close of day t from data <= t (see features/*).
- Labels start at the open of day t+1 (see backtest/labels.py).
- `purge_days` removes rows whose label window would overlap the next split.
- The model is flagged `calibrated=True` only if, on the untouched test window, it beats the
  base rate (Brier skill > 0) and its reliability bins deviate by at most `calibration_max_gap`.
  Only then may the bot display a probability.
"""

from __future__ import annotations

import json
import logging
import math
import pickle
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sqlalchemy import select, update

from bors_analyser.backtest.labels import target_hit, target_return
from bors_analyser.config import get_settings
from bors_analyser.db.models import ModelRegistry
from bors_analyser.db.session import session_scope
from bors_analyser.timeutil import now_tehran

log = logging.getLogger(__name__)

MODEL_FEATURES = [
    # tablo
    "volume_ratio_5", "volume_ratio_20", "volume_ratio_30", "value_ratio_20", "volume_zscore_60",
    "real_money_flow_ratio", "real_money_flow_ratio_5", "inflow_days_5", "buyer_power",
    "buyer_power_vs_prev5", "buyer_power_change_1d", "trade_count_ratio_20", "avg_trade_value_ratio_20",
    "close_return", "last_return", "last_vs_close", "gap", "dist_from_high", "dist_from_low",
    "momentum_5", "momentum_20", "momentum_60", "log_avg_value_20",
    # technical
    "rsi_14", "macd_hist_pct", "atr_pct", "bb_position", "volatility_20", "volatility_60", "volatility_ratio",
    "dist_sma_20", "dist_sma_50", "dist_sma_200", "dist_to_resistance_20", "dist_to_support_20",
    "dist_to_resistance_60", "breakout_20", "breakout_60", "breakdown_20", "sma_50_slope_10",
    # market context
    "market_breadth", "market_median_return", "market_money_flow_ratio",
]


@dataclass
class TargetSpec:
    id: str
    type: str  # hit | return
    threshold: float
    horizon: int
    fa: str = ""

    def expr(self) -> pl.Expr:
        f = target_hit if self.type == "hit" else target_return
        return f(self.threshold, self.horizon)

    def describe(self) -> str:
        what = "max high" if self.type == "hit" else "close return"
        return f"P({what} >= {self.threshold:+.0%} within {self.horizon} trading days, entry next open)"


def prepare_matrix(panel: pl.DataFrame) -> pl.DataFrame:
    """Add derived model columns; booleans -> 0/1; inf -> null."""
    df = panel.with_columns(pl.col("avg_value_20").log1p().alias("log_avg_value_20"))
    casts = []
    for c in MODEL_FEATURES:
        if c not in df.columns:
            casts.append(pl.lit(None, dtype=pl.Float64).alias(c))
        elif df.schema[c] == pl.Boolean:
            casts.append(pl.col(c).cast(pl.Float64))
        else:
            casts.append(pl.col(c).cast(pl.Float64, strict=False))
    df = df.with_columns(casts)
    return df.with_columns([pl.when(pl.col(c).is_infinite()).then(None).otherwise(pl.col(c)).alias(c)
                            for c in MODEL_FEATURES])


def _months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, 28))


def _xy(df: pl.DataFrame, target: str) -> tuple[np.ndarray, np.ndarray]:
    return df.select(MODEL_FEATURES).to_numpy(), df[target].to_numpy().astype(int)


def reliability_table(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    """Quantile bins of predicted probability: mean prediction vs observed frequency."""
    order = np.argsort(p)
    out = []
    for chunk in np.array_split(order, bins):
        if len(chunk) == 0:
            continue
        out.append({
            "p_lo": float(p[chunk].min()), "p_hi": float(p[chunk].max()),
            "predicted": float(p[chunk].mean()), "observed": float(y[chunk].mean()), "n": int(len(chunk)),
        })
    return out


def evaluate(p: np.ndarray, y: np.ndarray, returns: np.ndarray | None = None) -> dict[str, Any]:
    base = float(y.mean())
    brier = float(brier_score_loss(y, p))
    brier_base = float(brier_score_loss(y, np.full_like(p, base, dtype=float)))
    top = np.argsort(p)[-max(1, len(p) // 10):]
    out = {
        "n": int(len(y)),
        "base_rate": base,
        "auc": float(roc_auc_score(y, p)) if 0 < base < 1 else None,
        "brier": brier,
        "brier_base_rate": brier_base,
        "brier_skill": 1 - brier / brier_base if brier_base > 0 else None,
        "log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))),
        "top_decile_hit_rate": float(y[top].mean()),
        "top_decile_lift": float(y[top].mean() / base) if base > 0 else None,
    }
    if returns is not None:
        r = returns[~np.isnan(returns)] if returns.dtype.kind == "f" else returns
        rt = returns[top]
        rt = rt[~np.isnan(rt)]
        out["mean_return_all"] = float(np.mean(r)) if len(r) else None
        out["mean_return_top_decile"] = float(np.mean(rt)) if len(rt) else None
    return out


def train_target(panel: pl.DataFrame, spec: TargetSpec, cfg: dict[str, Any], min_avg_value: float) -> dict[str, Any]:
    df = prepare_matrix(panel).with_columns(spec.expr().alias("y"))
    df = df.filter(
        pl.col("y").is_not_null()
        & pl.col("entry_price").is_not_null()
        & ~pl.col("entry_unfillable").fill_null(False)
        & (pl.col("avg_value_20") >= min_avg_value)
    )
    last = df["date"].max()
    purge = timedelta(days=cfg.get("purge_days", 15))
    test_start = _months_before(last, cfg.get("test_months", 12))
    calib_start = _months_before(test_start, cfg.get("calibration_months", 12))
    train = df.filter(pl.col("date") < calib_start - purge)
    calib = df.filter((pl.col("date") >= calib_start) & (pl.col("date") < test_start - purge))
    test = df.filter(pl.col("date") >= test_start)
    if min(train.height, calib.height, test.height) < 1000:
        raise ValueError(f"not enough data: train={train.height} calib={calib.height} test={test.height}")

    dates = train["date"].unique().sort()
    valid_start = dates[int(len(dates) * 0.9)]
    fit = train.filter(pl.col("date") < valid_start - purge)
    valid = train.filter(pl.col("date") >= valid_start)
    Xf, yf = _xy(fit, "y")
    Xv, yv = _xy(valid, "y")
    params = dict(cfg["lgb_params"])
    booster = lgb.train(
        params, lgb.Dataset(Xf, yf, feature_name=MODEL_FEATURES),
        num_boost_round=cfg.get("num_boost_round", 600),
        valid_sets=[lgb.Dataset(Xv, yv, feature_name=MODEL_FEATURES)],
        callbacks=[lgb.early_stopping(cfg.get("early_stopping_rounds", 50), verbose=False)],
    )

    Xc, yc = _xy(calib, "y")
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(booster.predict(Xc), yc)

    Xt, yt = _xy(test, "y")
    raw_t = booster.predict(Xt)
    cal_t = iso.predict(raw_t)
    ret_col = f"ret_{spec.horizon}d"
    rets = test[ret_col].to_numpy().astype(float) if ret_col in test.columns else None
    metrics = {
        "test_raw": evaluate(raw_t, yt, rets),
        "test_calibrated": evaluate(cal_t, yt, rets),
        "best_iteration": booster.best_iteration,
        "splits": {"train": [str(train["date"].min()), str(train["date"].max()), train.height],
                   "calibration": [str(calib["date"].min()), str(calib["date"].max()), calib.height],
                   "test": [str(test["date"].min()), str(test["date"].max()), test.height]},
        "feature_importance_gain": dict(sorted(
            zip(MODEL_FEATURES, [float(x) for x in booster.feature_importance("gain")], strict=True),
            key=lambda kv: -kv[1])[:20]),
    }
    reliability = reliability_table(cal_t, yt)
    big = [b for b in reliability if b["n"] >= 500]
    max_gap = max((abs(b["predicted"] - b["observed"]) for b in big), default=math.inf)
    skill = metrics["test_calibrated"]["brier_skill"] or 0
    calibrated = bool(skill > 0 and max_gap <= cfg.get("calibration_max_gap", 0.05)
                      and test.height >= cfg.get("min_test_samples", 20000))
    metrics["calibration_check"] = {"max_bin_gap": max_gap, "brier_skill": skill, "passed": calibrated}
    return {"booster": booster, "calibrator": iso, "metrics": metrics, "reliability": reliability,
            "calibrated": calibrated, "train": train, "test": test}


def save_model(spec: TargetSpec, result: dict[str, Any]) -> str:
    settings = get_settings()
    model_id = f"{spec.id}-{now_tehran():%Y%m%d%H%M}-{uuid.uuid4().hex[:4]}"
    path = settings.model_dir / model_id
    path.mkdir(parents=True, exist_ok=True)
    result["booster"].save_model(str(path / "model.txt"))
    with (path / "calibrator.pkl").open("wb") as f:
        pickle.dump(result["calibrator"], f)
    meta = {"target": spec.__dict__, "features": MODEL_FEATURES, "metrics": result["metrics"],
            "reliability": result["reliability"], "calibrated": result["calibrated"]}
    (path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    sp = result["metrics"]["splits"]
    with session_scope() as s:
        s.execute(update(ModelRegistry).where(ModelRegistry.target == spec.id).values(active=False))
        s.add(ModelRegistry(
            model_id=model_id, target=spec.id, created_at=now_tehran().replace(tzinfo=None),
            train_start=date.fromisoformat(sp["train"][0]), train_end=date.fromisoformat(sp["train"][1]),
            test_start=date.fromisoformat(sp["test"][0]), test_end=date.fromisoformat(sp["test"][1]),
            features=MODEL_FEATURES, metrics=json.loads(json.dumps(result["metrics"], default=str)),
            reliability=result["reliability"], calibrated=result["calibrated"], path=str(path), active=True,
        ))
    return model_id


class LoadedModel:
    def __init__(self, reg: ModelRegistry):
        self.reg = reg
        path = Path(reg.path)
        self.booster = lgb.Booster(model_file=str(path / "model.txt"))
        with (path / "calibrator.pkl").open("rb") as f:
            self.calibrator: IsotonicRegression = pickle.load(f)
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        self.spec = TargetSpec(**meta["target"])

    def predict(self, df: pl.DataFrame) -> pl.DataFrame:
        X = prepare_matrix(df).select(MODEL_FEATURES).to_numpy()
        raw = self.booster.predict(X)
        cal = self.calibrator.predict(raw)
        return pl.DataFrame({"ins_code": df["ins_code"], "raw_score": raw, "probability": cal})

    def observed_accuracy_near(self, p: float) -> dict[str, float] | None:
        """Reliability bin (from the test window) that contains p."""
        for b in self.reg.reliability or []:
            if b["p_lo"] <= p <= b["p_hi"]:
                return {"predicted": b["predicted"], "observed": b["observed"], "n": b["n"]}
        return None


def active_models() -> list[LoadedModel]:
    with session_scope() as s:
        regs = s.execute(select(ModelRegistry).where(ModelRegistry.active.is_(True))).scalars().all()
    out = []
    for r in regs:
        try:
            out.append(LoadedModel(r))
        except Exception:
            log.exception("could not load model %s", r.model_id)
    return out


def model_outputs(models: list[LoadedModel], rows: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Per ins_code: {target_id: {...}} — probability only when the model is calibrated."""
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        preds = m.predict(rows)
        for r in preds.iter_rows(named=True):
            entry: dict[str, Any] = {
                "target": m.spec.id, "definition": m.spec.describe(), "definition_fa": m.spec.fa,
                "model_id": m.reg.model_id, "calibrated": bool(m.reg.calibrated),
                "model_score": round(100 * float(r["raw_score"]), 1),
            }
            if m.reg.calibrated:
                p = float(r["probability"])
                entry["probability"] = round(p, 3)
                entry["historical_bin"] = m.observed_accuracy_near(p)
                entry["test_window"] = [str(m.reg.test_start), str(m.reg.test_end)]
            out.setdefault(r["ins_code"], {})[m.spec.id] = entry
    return out
