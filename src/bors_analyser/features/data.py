"""Loading stored data into Polars frames for feature computation."""

from __future__ import annotations

from datetime import date

import polars as pl
from sqlalchemy import select

from bors_analyser.db.models import DailyClientType, DailyPrice, Instrument, IntradaySnapshot
from bors_analyser.db.session import get_engine


def _read(stmt) -> pl.DataFrame:
    with get_engine().connect() as conn:
        return pl.read_database(stmt, connection=conn, infer_schema_length=None)


def load_daily(ins_codes: list[str] | None = None, start: date | None = None) -> pl.DataFrame:
    """Daily prices joined with client type, sorted by (ins_code, date)."""
    q = select(
        DailyPrice.ins_code, DailyPrice.date, DailyPrice.open, DailyPrice.high, DailyPrice.low,
        DailyPrice.close, DailyPrice.last, DailyPrice.yesterday, DailyPrice.volume, DailyPrice.value,
        DailyPrice.trade_count, DailyPrice.adj_close, DailyPrice.source,
    )
    if ins_codes:
        q = q.where(DailyPrice.ins_code.in_(ins_codes))
    if start:
        q = q.where(DailyPrice.date >= start)
    prices = _read(q)

    cq = select(
        DailyClientType.ins_code, DailyClientType.date,
        DailyClientType.real_buy_value, DailyClientType.real_sell_value,
        DailyClientType.real_buy_count, DailyClientType.real_sell_count,
        DailyClientType.real_buy_volume, DailyClientType.real_sell_volume,
        DailyClientType.legal_buy_value, DailyClientType.legal_sell_value,
    )
    if ins_codes:
        cq = cq.where(DailyClientType.ins_code.in_(ins_codes))
    if start:
        cq = cq.where(DailyClientType.date >= start)
    ct = _read(cq)
    if prices.is_empty():
        return prices
    prices = prices.with_columns(pl.col("date").cast(pl.Date))
    if not ct.is_empty():
        ct = ct.with_columns(pl.col("date").cast(pl.Date))
        prices = prices.join(ct, on=["ins_code", "date"], how="left")
    return prices.sort("ins_code", "date")


def load_instruments(stocks_only: bool = True) -> pl.DataFrame:
    q = select(
        Instrument.ins_code, Instrument.symbol, Instrument.name, Instrument.market,
        Instrument.sector_code, Instrument.sector_name, Instrument.total_shares, Instrument.eps,
        Instrument.is_stock,
    )
    if stocks_only:
        q = q.where(Instrument.is_stock.is_(True))
    return _read(q)


def load_snapshots(trade_date: date, ins_codes: list[str] | None = None) -> pl.DataFrame:
    q = select(IntradaySnapshot).where(IntradaySnapshot.trade_date == trade_date)
    if ins_codes:
        q = q.where(IntradaySnapshot.ins_code.in_(ins_codes))
    df = _read(q)
    return df.sort("ins_code", "ts") if not df.is_empty() else df
