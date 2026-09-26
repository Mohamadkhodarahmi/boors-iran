"""Push results to the Cloudflare Worker (authenticated ingest endpoints).

The local machine is not publicly reachable, so it POSTs to the Worker. Requests go through the
outbound proxy (Cloudflare may be unreachable directly from Iran) except for localhost targets.
The ingest token is only ever sent in the Authorization header.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from bors_analyser.analysis import fmt
from bors_analyser.config import get_settings
from bors_analyser.db.models import CodalLetter
from bors_analyser.db.session import FetchAudit, session_scope
from bors_analyser.net import outbound_proxy
from bors_analyser.timeutil import now_tehran

log = logging.getLogger(__name__)


class PublisherNotConfigured(RuntimeError):
    pass


def _client() -> httpx.Client:
    s = get_settings()
    if not s.worker_url or not s.ingest_token:
        raise PublisherNotConfigured("set BORS_WORKER_URL and BORS_INGEST_TOKEN in .env")
    host = urlparse(s.worker_url).hostname or ""
    proxy = None if host in ("localhost", "127.0.0.1") else outbound_proxy()
    return httpx.Client(
        base_url=s.worker_url.rstrip("/"), proxy=proxy, trust_env=False,
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers={"Authorization": f"Bearer {s.ingest_token}", "content-type": "application/json"},
    )


def _post(client: httpx.Client, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    with FetchAudit("cloudflare", f"POST {path}") as audit:
        r = client.post(path, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
        audit.rows = len(payload.get("items") or []) or 1
        return r.json()


def analysis_items(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for ev in evs:
        sc = ev["scores"]
        items.append({
            "symbol": ev["symbol"], "name": ev.get("name"), "sector": ev.get("sector"), "market": ev.get("market"),
            "mode": ev["mode"], "asof": ev["asof"], "data_time": ev.get("data_time"), "label": sc.get("label"),
            "overall_score": sc.get("overall_score"), "risk_score": sc.get("risk_score"),
            "scores": {k: v for k, v in sc.items() if k != "components"},
            "sections": ev["report"]["sections"], "text": ev["telegram_text"],
            "model": ev.get("model") or {},
        })
    return items


def market_text(scan: dict[str, Any]) -> str:
    m = scan.get("market") or {}
    idx = m.get("index") or {}
    lines = ["━━━━━━━━━━━━━━━━━━", "📊 وضعیت بازار", "━━━━━━━━━━━━━━━━━━"]
    if idx.get("tse_index") is not None:
        chg = idx.get("tse_index_change")
        prev = idx["tse_index"] - chg if chg is not None else None
        change = f" ({fmt.pct(chg / prev, 2)})" if chg is not None and prev else ""
        lines.append(f"شاخص کل: {idx['tse_index']:,.0f}{change}")
    if idx.get("tse_tval") is not None:
        lines.append(f"ارزش معاملات بورس: {fmt.money(idx['tse_tval'], signed=False)}")
    if idx.get("fb_tval") is not None:
        lines.append(f"ارزش معاملات فرابورس: {fmt.money(idx['fb_tval'], signed=False)}")
    if m.get("breadth") is not None:
        lines.append(f"سهم‌های مثبت (از نقدشونده‌ها): {m['breadth'] * 100:.0f}٪")
    if m.get("median_return") is not None:
        lines.append(f"میانه تغییر قیمت پایانی: {fmt.pct(m['median_return'], 2)}")
    if m.get("real_money_flow") is not None:
        lines.append(f"برآیند پول حقیقی: {fmt.money(m['real_money_flow'])}")
    cats = scan.get("categories") or {}
    if cats:
        lines.append("")
        lines += [f"{c['icon']} {c['fa']}: {c.get('count', 0)}" for c in cats.values()]
    lines += ["", f"⏱ {idx.get('datetime') or scan.get('asof')}", "داده‌ها از TSETMC؛ این گزارش توصیه سرمایه‌گذاری نیست."]
    return "\n".join(lines)


def top_text(scan: dict[str, Any], n: int = 10) -> str:
    lines = ["🏆 بالاترین Model Score (سوئینگ)", "Model Score احتمال نیست.", ""]
    for i, t in enumerate(scan.get("top", [])[:n], 1):
        lines.append(f"{i}. {t['symbol']} — {t['overall_score'] or 0:.0f} — {t['label_fa']}")
    lines += ["", "این فهرست توصیه خرید یا فروش نیست."]
    return "\n".join(lines)


def recent_codal(days: int = 7) -> list[dict[str, Any]]:
    since = now_tehran().replace(tzinfo=None) - timedelta(days=days)
    with session_scope() as s:
        rows = s.execute(select(CodalLetter).where(CodalLetter.published_at >= since,
                                                   CodalLetter.ins_code.is_not(None))).scalars().all()
        return [{"tracing_no": L.tracing_no, "symbol": L.symbol, "title": L.title[:1000],
                 "report_type": L.report_type, "published_at": L.published_at.isoformat() if L.published_at else None,
                 "url": L.url} for L in rows]


def publish(evs: list[dict[str, Any]], scan: dict[str, Any] | None = None, chunk: int = 300) -> dict[str, Any]:
    from bors_analyser.scanner import scan_text

    stats: dict[str, Any] = {}
    with _client() as c:
        items = analysis_items(evs)
        for i in range(0, len(items), chunk):
            _post(c, "/api/ingest/analysis", {"items": items[i:i + chunk]})
        stats["analysis"] = len(items)
        if scan:
            asof = scan.get("asof") or datetime.now().isoformat()
            _post(c, "/api/ingest/market", {"asof": asof, "data": scan.get("market"), "text": market_text(scan),
                                            "date": asof[:10], "summary": market_text(scan)})
            _post(c, "/api/ingest/signals", {"asof": asof, "data": scan, "text": scan_text(scan),
                                             "top_text": top_text(scan)})
            stats["scan"] = True
        codal = recent_codal()
        if codal:
            _post(c, "/api/ingest/codal", {"items": codal})
        stats["codal"] = len(codal)
    log.info("published: %s", stats)
    return stats
