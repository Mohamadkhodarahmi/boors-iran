"""Market-wide scan: categorise every liquid stock from its latest analysis evidence."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from bors_analyser.analysis.engine import analyze
from bors_analyser.config import get_scoring_config
from bors_analyser.db.models import Analysis, MarketSnapshot
from bors_analyser.db.session import session_scope

log = logging.getLogger(__name__)

CATEGORIES = {
    "unusual_volume": {"icon": "🔥", "fa": "حجم غیرعادی", "sort": ("tablo", "volume_ratio_20")},
    "real_money_inflow": {"icon": "💰", "fa": "ورود پول حقیقی", "sort": ("tablo", "real_money_flow")},
    "strong_buyer": {"icon": "🟢", "fa": "قدرت خریدار بالا", "sort": ("tablo", "buyer_power")},
    "breakout": {"icon": "📈", "fa": "کاندید شکست مقاومت", "sort": ("tablo", "volume_ratio_20")},
    "fundamental_improvement": {"icon": "🔵", "fa": "بهبود بنیادی", "sort": ("scores", "fundamental_score")},
    "risk_divergence": {"icon": "⚠️", "fa": "ریسک / واگرایی", "sort": ("scores", "risk_score")},
}


def categorize(ev: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    sc = cfg["scanner"]
    t, x, s = ev.get("tablo") or {}, ev.get("technical") or {}, ev.get("scores") or {}
    tags = []
    if (t.get("volume_ratio_20") or 0) >= sc["unusual_volume"]["volume_ratio_20"]:
        tags.append("unusual_volume")
    rmi = sc["real_money_inflow"]
    if (t.get("real_money_flow_ratio") or 0) >= rmi["real_money_flow_ratio"] and \
            (t.get("real_money_flow") or 0) >= rmi["min_flow_rial"]:
        tags.append("real_money_inflow")
    if (t.get("buyer_power") or 0) >= sc["strong_buyer"]["buyer_power"]:
        tags.append("strong_buyer")
    if x.get("breakout_20") and (t.get("volume_ratio_20") or 0) >= sc["breakout"]["volume_ratio_20"]:
        tags.append("breakout")
    if (s.get("fundamental_score") or 0) >= sc["fundamental_improvement"]["fundamental_score"]:
        tags.append("fundamental_improvement")
    div = (ev.get("divergence") or {}).get("code")
    if (s.get("risk_score") or 0) >= sc["risk_divergence"]["risk_score"] or \
            div in ("market_fundamental_divergence", "trend_without_money"):
        tags.append("risk_divergence")
    return tags


def market_summary(evs: list[dict[str, Any]]) -> dict[str, Any]:
    with session_scope() as s:
        snap = s.execute(select(MarketSnapshot).order_by(MarketSnapshot.ts.desc()).limit(1)).scalars().first()
    mc = next((ev.get("market_context") for ev in evs if ev.get("market_context")), {}) or {}
    return {
        "index": (snap.data if snap else {}),
        "breadth": mc.get("market_breadth"),
        "median_return": mc.get("market_median_return"),
        "real_money_flow": mc.get("market_real_money_flow"),
        "real_money_flow_ratio": mc.get("market_money_flow_ratio"),
        "analysed": len(evs),
    }


def scan(persist: bool = True, evs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Categorise stocks. Pass `evs` (from `analyze`) to reuse an existing run; swing mode is used."""
    cfg = get_scoring_config()
    min_value = cfg["universe"]["min_avg_value_20"]
    if evs is None:
        evs = analyze(modes=("swing",), persist=persist)
    evs = [ev for ev in evs if ev["mode"] == "swing"]
    liquid = [ev for ev in evs if (ev["tablo"].get("avg_value_20") or 0) >= min_value]
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in CATEGORIES}
    for ev in liquid:
        ev["scan_tags"] = categorize(ev, cfg)
        for tag in ev["scan_tags"]:
            buckets[tag].append(ev)
    top_n = cfg["scanner"]["top_n"]
    out_buckets = {}
    for tag, items in buckets.items():
        group, key = CATEGORIES[tag]["sort"]
        items.sort(key=lambda e: (e.get(group) or {}).get(key) or 0, reverse=True)
        out_buckets[tag] = [{
            "symbol": e["symbol"], "name": e["name"], "label": e["scores"]["label"],
            "overall_score": e["scores"]["overall_score"], "risk_score": e["scores"]["risk_score"],
            "volume_ratio_20": e["tablo"].get("volume_ratio_20"), "buyer_power": e["tablo"].get("buyer_power"),
            "real_money_flow": e["tablo"].get("real_money_flow"), "last_return": e["tablo"].get("last_return"),
        } for e in items[:top_n]]
    ranking = sorted(liquid, key=lambda e: e["scores"].get("overall_score") or 0, reverse=True)[:top_n]
    result = {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "universe": {"analysed": len(evs), "liquid": len(liquid), "min_avg_value_20": min_value},
        "market": market_summary(liquid),
        "categories": {k: {**{kk: vv for kk, vv in CATEGORIES[k].items() if kk != "sort"},
                           "count": len(buckets[k]), "items": v}
                       for k, v in out_buckets.items()},
        "top": [{"symbol": e["symbol"], "name": e["name"], "label": e["scores"]["label"],
                 "label_fa": e["scores"]["fa"], "overall_score": e["scores"]["overall_score"]} for e in ranking],
    }
    if persist:
        with session_scope() as s:
            for ev in liquid:
                row = s.get(Analysis, (ev["ins_code"], _asof(ev), "swing"))
                if row:
                    row.scan_tags = ev["scan_tags"]
    log.info("scan: %d liquid of %d; %s", len(liquid), len(evs),
             {k: len(v) for k, v in buckets.items()})
    return result


def _asof(ev: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(ev["asof"])


def scan_text(result: dict[str, Any], per_category: int = 5) -> str:
    """Telegram-ready scan summary."""
    lines = ["🔎 اسکن بازار", f"سهم‌های نقدشونده بررسی‌شده: {result['universe']['liquid']}", ""]
    for key, cat in result["categories"].items():
        items = cat["items"][:per_category]
        lines.append(f"{cat['icon']} {cat['fa']} ({cat.get('count', len(cat['items']))})")
        if key == "risk_divergence":
            lines += [f"  • {it['symbol']} — ریسک {it['risk_score'] or 0:.0f}" for it in items] or ["  —"]
        else:
            lines += [f"  • {it['symbol']} — امتیاز {it['overall_score'] or 0:.0f}" for it in items] or ["  —"]
        lines.append("")
    lines.append("این فهرست خروجی خودکار داده‌هاست و توصیه خرید یا فروش نیست.")
    return "\n".join(lines)
