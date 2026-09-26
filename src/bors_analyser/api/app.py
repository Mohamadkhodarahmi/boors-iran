"""Local read-only API (for a future dashboard). Mirrors the Worker's GET endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import func, select

from bors_analyser.config import PROJECT_ROOT
from bors_analyser.db.models import Analysis, CodalLetter, Instrument, MarketSnapshot
from bors_analyser.db.session import init_db, session_scope
from bors_analyser.timeutil import normalize_fa

LATEST_SCAN = PROJECT_ROOT / "data" / "latest_scan.json"

app = FastAPI(title="Iranian Market Intelligence Engine", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _latest_analysis(symbol: str, mode: str) -> Analysis:
    with session_scope() as s:
        row = s.execute(
            select(Analysis).where(Analysis.symbol == normalize_fa(symbol), Analysis.mode == mode)
            .order_by(Analysis.asof.desc()).limit(1)
        ).scalars().first()
    if row is None:
        raise HTTPException(404, "symbol not analysed (data unavailable)")
    return row


@app.get("/health")
def health() -> dict[str, Any]:
    with session_scope() as s:
        last = s.execute(select(func.max(Analysis.asof))).scalar()
    return {"ok": True, "last_analysis": last}


@app.get("/market")
def market() -> dict[str, Any]:
    with session_scope() as s:
        snap = s.execute(select(MarketSnapshot).order_by(MarketSnapshot.ts.desc()).limit(1)).scalars().first()
    if snap is None:
        raise HTTPException(404, "no market snapshot yet")
    out: dict[str, Any] = {"ts": snap.ts, "index": snap.data}
    if LATEST_SCAN.exists():
        out["scan_market"] = json.loads(LATEST_SCAN.read_text(encoding="utf-8")).get("market")
    return out


@app.get("/stocks")
def stocks() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(select(Instrument).where(Instrument.is_stock.is_(True)).order_by(Instrument.symbol)).scalars()
        return [{"symbol": r.symbol, "name": r.name, "sector": r.sector_name, "market": r.market,
                 "ins_code": r.ins_code} for r in rows]


@app.get("/stock/{symbol}")
def stock(symbol: str) -> dict[str, Any]:
    with session_scope() as s:
        inst = s.execute(select(Instrument).where(Instrument.symbol == normalize_fa(symbol))).scalars().first()
        if inst is None:
            raise HTTPException(404, "unknown symbol")
        letters = s.execute(select(CodalLetter).where(CodalLetter.symbol == inst.symbol)
                            .order_by(CodalLetter.published_at.desc()).limit(10)).scalars().all()
        return {"symbol": inst.symbol, "name": inst.name, "sector": inst.sector_name, "market": inst.market,
                "isin": inst.isin, "ins_code": inst.ins_code,
                "recent_codal": [{"title": L.title, "type": L.report_type, "published_at": L.published_at,
                                  "url": L.url} for L in letters]}


@app.get("/analysis/{symbol}")
def analysis(symbol: str, mode: str = Query("swing", pattern="^(short|swing|fundamental)$")) -> dict[str, Any]:
    row = _latest_analysis(symbol, mode)
    return {"symbol": row.symbol, "mode": row.mode, "asof": row.asof, "label": row.label, "scores": row.scores,
            "probability": row.probability, "narrative": row.narrative, "risks": row.risks,
            "scan_tags": row.scan_tags, "evidence": row.evidence}


def _section(symbol: str, mode: str, key: str) -> dict[str, Any]:
    row = _latest_analysis(symbol, mode)
    ev = row.evidence or {}
    return {"symbol": row.symbol, "asof": row.asof, "data_time": ev.get("data_time"), key: ev.get(key),
            "text": (ev.get("report") or {}).get("sections", {}).get(key)}


@app.get("/tablo/{symbol}")
def tablo(symbol: str) -> dict[str, Any]:
    out = _section(symbol, "short", "tablo")
    ev = _latest_analysis(symbol, "short").evidence or {}
    out.update({"orderbook": ev.get("orderbook"), "intraday": ev.get("intraday")})
    return out


@app.get("/technical/{symbol}")
def technical(symbol: str) -> dict[str, Any]:
    return _section(symbol, "swing", "technical")


@app.get("/fundamental/{symbol}")
def fundamental(symbol: str) -> dict[str, Any]:
    return _section(symbol, "fundamental", "fundamental")


@app.get("/signals")
def signals() -> dict[str, Any]:
    if not LATEST_SCAN.exists():
        raise HTTPException(404, "no scan yet")
    return json.loads(LATEST_SCAN.read_text(encoding="utf-8"))
