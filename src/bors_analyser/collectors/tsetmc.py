"""TSETMC collector.

Thin adapter around the `tsetmc` library (5j9/tsetmc). This module only fetches and
normalizes data into our schema; it never computes features. Pure `build_*` functions
turn library DataFrames into row dicts so they can be tested on fixtures.

Field notes (verified against live data 2026-09-23):
- cdn ClientType: `I` = حقیقی (individual), `N` = حقوقی (institutional).
- old ClientTypeAll: `n_` = natural = حقیقی, `l_` = legal = حقوقی. Volumes and counts only.
- market watch `pc` = closing price (قیمت پایانی), `pl` = last trade price, `py` = yesterday.
- `GetTradeHistory` under-reports volume (~36%); daily totals come from ClosingPrice endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

import polars as pl
import tsetmc
from tsetmc import market_watch as mw
from tsetmc.instruments import Instrument as TseInstrument

from bors_analyser.config import get_settings
from bors_analyser.db.models import (
    DailyClientType,
    DailyPrice,
    Instrument,
    IntradaySnapshot,
    MarketSnapshot,
)
from bors_analyser.db.session import FetchAudit, session_scope, upsert
from bors_analyser.net import iran_client
from bors_analyser.timeutil import deven_to_date, normalize_fa, now_tehran

log = logging.getLogger(__name__)

FLOW_NAMES = {1: "بورس", 2: "فرابورس", 3: "مشتقه", 4: "پایه فرابورس", 5: "پایه", 6: "بورس انرژی", 7: "بورس کالا"}


def _naive_now() -> datetime:
    return now_tehran().replace(tzinfo=None, microsecond=0)


@asynccontextmanager
async def tsetmc_session() -> AsyncIterator[None]:
    """The library keeps one global aiohttp session bound to the running loop; reset it per job."""
    try:
        yield
    finally:
        sm = tsetmc.session_manager
        await sm.aclose()
        sm._session = None


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pos(v: Any) -> float | None:
    f = _f(v)
    return f if f is not None and f > 0 else None


def _i(v: Any) -> int | None:
    f = _f(v)
    return None if f is None else int(f)


# --------------------------------------------------------------------------- sectors


async def fetch_sector_names() -> dict[str, str]:
    async with iran_client() as client:
        r = await client.get("https://cdn.tsetmc.com/api/StaticData/GetStaticData")
        r.raise_for_status()
        items = r.json()["staticData"]
    return {
        f"{int(x['code']):02d}": normalize_fa(x["name"])
        for x in items
        if x["type"] == "IndustrialGroup"
    }


# --------------------------------------------------------------------------- market watch


def build_instrument_rows(
    prices: pl.DataFrame, sectors: dict[str, str], stock_yvals: list[str], now: datetime
) -> list[dict[str, Any]]:
    rows = []
    for r in prices.iter_rows(named=True):
        cs = r.get("cs")
        sector_code = f"{int(cs):02d}" if cs not in (None, "") and str(cs).strip().isdigit() else None
        flow = _i(r.get("flow"))
        rows.append(
            {
                "ins_code": str(r["ins_code"]),
                "symbol": normalize_fa(r.get("l18")),
                "name": normalize_fa(r.get("l30")),
                "isin": r.get("isin"),
                "flow": flow,
                "market": FLOW_NAMES.get(flow or -1),
                "board": r.get("cgrvalcot"),
                "yval": str(r.get("yval")) if r.get("yval") is not None else None,
                "sector_code": sector_code,
                "sector_name": sectors.get(sector_code or ""),
                "is_stock": str(r.get("yval")) in stock_yvals,
                "base_volume": _i(r.get("bvol")),
                "total_shares": _i(r.get("z")),
                "eps": _f(r.get("eps")),
                "source": "tsetmc",
                "last_updated": now,
            }
        )
    return rows


def build_snapshot_rows(
    prices: pl.DataFrame, client_type: pl.DataFrame | None, ts: datetime, trade_date: date
) -> list[dict[str, Any]]:
    """Merge market-watch prices + best limits with ClientTypeAll into snapshot rows."""
    ct: dict[str, dict[str, Any]] = {}
    if client_type is not None:
        for r in client_type.iter_rows(named=True):
            ct[str(r["ins_code"])] = r
    rows = []
    for r in prices.iter_rows(named=True):
        code = str(r["ins_code"])
        c = ct.get(code)
        tvol = _f(r.get("tvol"))
        quality = "ok" if c is not None else ("partial" if (tvol or 0) > 0 else "ok")
        rows.append(
            {
                "ins_code": code,
                "ts": ts,
                "trade_date": trade_date,
                "tsetmc_time": _i(r.get("heven")),
                "open": _f(r.get("pf")),
                "high": _f(r.get("pmax")),
                "low": _f(r.get("pmin")),
                "last": _f(r.get("pl")),
                "close": _f(r.get("pc")),
                "yesterday": _f(r.get("py")),
                "volume": tvol,
                "value": _f(r.get("tval")),
                "trade_count": _i(r.get("tno")),
                "price_limit_high": _f(r.get("tmax")),
                "price_limit_low": _f(r.get("tmin")),
                "real_buy_volume": _f(c["n_buy_volume"]) if c else None,
                "real_sell_volume": _f(c["n_sell_volume"]) if c else None,
                "legal_buy_volume": _f(c["l_buy_volume"]) if c else None,
                "legal_sell_volume": _f(c["l_sell_volume"]) if c else None,
                "real_buy_count": _i(c["n_buy_count"]) if c else None,
                "real_sell_count": _i(c["n_sell_count"]) if c else None,
                "legal_buy_count": _i(c["l_buy_count"]) if c else None,
                "legal_sell_count": _i(c["l_sell_count"]) if c else None,
                "bid_prices": [_f(r.get(f"pd{i}")) for i in range(1, 6)],
                "bid_volumes": [_f(r.get(f"qd{i}")) for i in range(1, 6)],
                "bid_counts": [_i(r.get(f"zd{i}")) for i in range(1, 6)],
                "ask_prices": [_f(r.get(f"po{i}")) for i in range(1, 6)],
                "ask_volumes": [_f(r.get(f"qo{i}")) for i in range(1, 6)],
                "ask_counts": [_i(r.get(f"zo{i}")) for i in range(1, 6)],
                "source": "tsetmc",
                "data_quality": quality,
            }
        )
    return rows


async def fetch_market_watch() -> tuple[pl.DataFrame, pl.DataFrame | None, dict[str, Any]]:
    """Returns (prices+best limits, client type all, market state)."""
    with FetchAudit("tsetmc", "market_watch_init") as audit:
        init = await mw.market_watch_init()
        prices = init["prices"].collect()
        audit.rows = prices.height
    client_type = None
    try:
        with FetchAudit("tsetmc", "client_type_all") as audit:
            client_type = (await mw.client_type_all()).collect()
            audit.rows = client_type.height
    except Exception:
        log.warning("client_type_all unavailable; snapshot stored without client type")
    return prices, client_type, init.get("market_state") or {}


async def collect_snapshot(stocks_only: bool = True) -> int:
    """Fetch one market-watch snapshot and store instruments + intraday rows + market state."""
    settings = get_settings()
    ts = _naive_now()
    async with tsetmc_session():
        prices, client_type, market_state = await fetch_market_watch()
    try:
        sectors = await fetch_sector_names()
    except Exception as e:
        log.warning("sector names unavailable: %s", e)
        sectors = {}

    inst_rows = build_instrument_rows(prices, sectors, settings.stock_yvals, ts)
    if not sectors:  # keep previously stored sector names
        for r in inst_rows:
            r.pop("sector_name")
    if stocks_only:
        prices = prices.filter(pl.col("yval").cast(pl.String).is_in(settings.stock_yvals))

    state_dt = market_state.get("datetime") if market_state else None
    trade_date = state_dt.date() if isinstance(state_dt, datetime) else ts.date()
    snap_rows = build_snapshot_rows(prices, client_type, ts, trade_date)

    with session_scope() as s:
        upsert(s, Instrument, inst_rows)
        upsert(s, IntradaySnapshot, snap_rows)
        if market_state:
            state = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in market_state.items()}
            upsert(s, MarketSnapshot, [{"ts": ts, "data": state, "source": "tsetmc"}])
    log.info("snapshot %s: %d instruments, %d stock snapshots", ts, len(inst_rows), len(snap_rows))
    return len(snap_rows)


# --------------------------------------------------------------------------- daily history


def build_daily_price_rows(
    ins_code: str, raw: pl.DataFrame, adjusted: pl.DataFrame | None, now: datetime
) -> list[dict[str, Any]]:
    adj_by_date: dict[date, float] = {}
    if adjusted is not None and adjusted.height:
        for r in adjusted.select("date", "pc").iter_rows():
            d = r[0].date() if isinstance(r[0], datetime) else r[0]
            adj_by_date[d] = _f(r[1])
    rows = []
    for r in raw.iter_rows(named=True):
        d = deven_to_date(r["dEven"])
        volume = _f(r.get("qTotTran5J"))
        if not volume:  # no trade that day (halted / holiday rows)
            continue
        rows.append(
            {
                "ins_code": ins_code,
                "date": d,
                # TSETMC occasionally reports 0 for these on traded days; store as missing, not 0.
                "open": _pos(r.get("priceFirst")),
                "high": _pos(r.get("priceMax")),
                "low": _pos(r.get("priceMin")),
                "close": _f(r.get("pClosing")),
                "last": _f(r.get("pDrCotVal")),
                "yesterday": _f(r.get("priceYesterday")),
                "volume": volume,
                "value": _f(r.get("qTotCap")),
                "trade_count": _i(r.get("zTotTran")),
                "adj_close": adj_by_date.get(d),
                "source": "tsetmc",
                "fetched_at": now,
            }
        )
    return rows


def build_client_type_rows(ins_code: str, hist: pl.DataFrame, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for r in hist.iter_rows(named=True):
        rows.append(
            {
                "ins_code": ins_code,
                "date": deven_to_date(r["recDate"]),
                "real_buy_volume": _f(r.get("buy_I_Volume")),
                "real_buy_value": _f(r.get("buy_I_Value")),
                "real_buy_count": _i(r.get("buy_I_Count")),
                "real_sell_volume": _f(r.get("sell_I_Volume")),
                "real_sell_value": _f(r.get("sell_I_Value")),
                "real_sell_count": _i(r.get("sell_I_Count")),
                "legal_buy_volume": _f(r.get("buy_N_Volume")),
                "legal_buy_value": _f(r.get("buy_N_Value")),
                "legal_buy_count": _i(r.get("buy_N_Count")),
                "legal_sell_volume": _f(r.get("sell_N_Volume")),
                "legal_sell_value": _f(r.get("sell_N_Value")),
                "legal_sell_count": _i(r.get("sell_N_Count")),
                "source": "tsetmc",
                "fetched_at": now,
            }
        )
    return rows


async def fetch_history(ins_code: str, symbol: str = "") -> tuple[int, int]:
    """Full daily price + adjusted close + client-type history for one instrument."""
    now = _naive_now()
    inst = TseInstrument(int(ins_code))
    target = f"{symbol}:{ins_code}"
    with FetchAudit("tsetmc", "daily_closing_price", target) as a:
        raw = (await inst.daily_closing_price(n=0)).collect()
        a.rows = raw.height
    adjusted = None
    try:
        with FetchAudit("tsetmc", "price_history_adjusted", target) as a:
            adjusted = (await inst.price_history(adjusted=True)).collect()
            a.rows = adjusted.height
    except Exception:
        log.warning("adjusted history unavailable for %s", target)
    ct_rows: list[dict[str, Any]] = []
    try:
        with FetchAudit("tsetmc", "client_type_history", target) as a:
            hist = (await inst.client_type_history()).collect()
            a.rows = hist.height
        ct_rows = build_client_type_rows(ins_code, hist, now)
    except Exception:
        log.warning("client type history unavailable for %s", target)

    price_rows = build_daily_price_rows(ins_code, raw, adjusted, now)
    with session_scope() as s:
        upsert(s, DailyPrice, price_rows)
        upsert(s, DailyClientType, ct_rows)
    return len(price_rows), len(ct_rows)


async def backfill(ins_codes: list[tuple[str, str]]) -> dict[str, Any]:
    """Fetch history for many instruments concurrently; one failure never stops the rest."""
    sem = asyncio.Semaphore(get_settings().tsetmc_concurrency)
    ok, failed = 0, []

    async def one(code: str, sym: str) -> None:
        nonlocal ok
        async with sem:
            try:
                await fetch_history(code, sym)
                ok += 1
            except Exception as e:
                failed.append((sym, code, repr(e)[:200]))

    async with tsetmc_session():
        await asyncio.gather(*(one(c, s) for c, s in ins_codes))
    if failed:
        log.warning("history failed for %d instruments: %s", len(failed), failed[:10])
    return {"ok": ok, "failed": failed}


def build_eod_rows_from_snapshots(snaps: list[IntradaySnapshot], now: datetime) -> tuple[list[dict], list[dict]]:
    """Provisional daily rows for today from the last snapshot of each instrument.

    TSETMC's daily list only includes today after the day is archived. Client-type *values*
    are not in the market-watch feed, so they are derived as volume × VWAP and tagged with
    source='tsetmc_snapshot'. The next official backfill overwrites these rows.
    """
    prices, cts = [], []
    for sn in snaps:
        if not sn.volume:
            continue
        vwap = (sn.value / sn.volume) if sn.value else None
        prices.append({
            "ins_code": sn.ins_code, "date": sn.trade_date, "open": sn.open, "high": sn.high,
            "low": sn.low, "close": sn.close, "last": sn.last, "yesterday": sn.yesterday,
            "volume": sn.volume, "value": sn.value, "trade_count": sn.trade_count,
            "adj_close": None, "source": "tsetmc_snapshot", "fetched_at": now,
        })
        if sn.real_buy_volume is None:
            continue

        def val(v: float | None, vwap: float | None = vwap) -> float | None:
            return None if v is None or vwap is None else v * vwap

        cts.append({
            "ins_code": sn.ins_code, "date": sn.trade_date,
            "real_buy_volume": sn.real_buy_volume, "real_buy_value": val(sn.real_buy_volume),
            "real_buy_count": sn.real_buy_count,
            "real_sell_volume": sn.real_sell_volume, "real_sell_value": val(sn.real_sell_volume),
            "real_sell_count": sn.real_sell_count,
            "legal_buy_volume": sn.legal_buy_volume, "legal_buy_value": val(sn.legal_buy_volume),
            "legal_buy_count": sn.legal_buy_count,
            "legal_sell_volume": sn.legal_sell_volume, "legal_sell_value": val(sn.legal_sell_volume),
            "legal_sell_count": sn.legal_sell_count,
            "source": "tsetmc_snapshot", "fetched_at": now,
        })
    return prices, cts


def finalize_day(trade_date: date | None = None) -> int:
    """Write provisional daily rows for `trade_date` where no official row exists yet."""
    from sqlalchemy import func, select

    now = _naive_now()
    with session_scope() as s:
        if trade_date is None:
            trade_date = s.execute(select(func.max(IntradaySnapshot.trade_date))).scalar()
        if trade_date is None:
            return 0
        last_ts = (
            select(IntradaySnapshot.ins_code, func.max(IntradaySnapshot.ts).label("ts"))
            .where(IntradaySnapshot.trade_date == trade_date)
            .group_by(IntradaySnapshot.ins_code)
            .subquery()
        )
        snaps = s.execute(
            select(IntradaySnapshot).join(
                last_ts,
                (IntradaySnapshot.ins_code == last_ts.c.ins_code) & (IntradaySnapshot.ts == last_ts.c.ts),
            )
        ).scalars().all()
        official = set(
            s.execute(
                select(DailyPrice.ins_code).where(
                    DailyPrice.date == trade_date, DailyPrice.source == "tsetmc"
                )
            ).scalars()
        )
        snaps = [sn for sn in snaps if sn.ins_code not in official]
        prices, cts = build_eod_rows_from_snapshots(snaps, now)
        upsert(s, DailyPrice, prices)
        upsert(s, DailyClientType, cts)
    log.info("finalize_day %s: %d provisional daily rows", trade_date, len(prices))
    return len(prices)


async def resolve_symbol(symbol: str) -> str | None:
    """Symbol (l18) -> ins_code via the library search."""
    async with tsetmc_session():
        try:
            inst = await TseInstrument.from_l18(symbol)
        except Exception as e:
            log.warning("could not resolve %s: %s", symbol, e)
            return None
    return str(inst.code)
