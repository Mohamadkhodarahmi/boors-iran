"""Relational schema (dialect-neutral: SQLite for the MVP, PostgreSQL in production).

Conventions
- Money in Rial, volume in shares.
- All DateTime columns are **naive Tehran local time** (market data is published in Tehran time).
- `source` / `fetched_at` / `data_quality` columns make data provenance auditable.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Instrument(Base):
    __tablename__ = "instruments"

    ins_code: Mapped[str] = mapped_column(String(24), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)  # l18 (normalized)
    name: Mapped[str | None] = mapped_column(String(255))  # l30
    isin: Mapped[str | None] = mapped_column(String(16), index=True)
    flow: Mapped[int | None] = mapped_column(Integer)  # 1 bourse, 2 farabourse, ...
    market: Mapped[str | None] = mapped_column(String(64))
    board: Mapped[str | None] = mapped_column(String(8))  # cgrValCot
    yval: Mapped[str | None] = mapped_column(String(8))
    sector_code: Mapped[str | None] = mapped_column(String(8), index=True)
    sector_name: Mapped[str | None] = mapped_column(String(128))
    is_stock: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    base_volume: Mapped[int | None] = mapped_column(BigInteger)
    total_shares: Mapped[int | None] = mapped_column(BigInteger)
    eps: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="tsetmc")
    last_updated: Mapped[datetime | None] = mapped_column(DateTime)


class DailyPrice(Base):
    __tablename__ = "daily_prices"

    ins_code: Mapped[str] = mapped_column(String(24), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)  # قیمت پایانی (pClosing)
    last: Mapped[float | None] = mapped_column(Float)  # آخرین قیمت (pDrCotVal)
    yesterday: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    value: Mapped[float | None] = mapped_column(Float)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    adj_close: Mapped[float | None] = mapped_column(Float)  # adjusted for capital changes / DPS
    source: Mapped[str] = mapped_column(String(32), default="tsetmc")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class DailyClientType(Base):
    __tablename__ = "daily_client_type"

    ins_code: Mapped[str] = mapped_column(String(24), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    # real = حقیقی (individual), legal = حقوقی (institutional)
    real_buy_volume: Mapped[float | None] = mapped_column(Float)
    real_buy_value: Mapped[float | None] = mapped_column(Float)
    real_buy_count: Mapped[int | None] = mapped_column(Integer)
    real_sell_volume: Mapped[float | None] = mapped_column(Float)
    real_sell_value: Mapped[float | None] = mapped_column(Float)
    real_sell_count: Mapped[int | None] = mapped_column(Integer)
    legal_buy_volume: Mapped[float | None] = mapped_column(Float)
    legal_buy_value: Mapped[float | None] = mapped_column(Float)
    legal_buy_count: Mapped[int | None] = mapped_column(Integer)
    legal_sell_volume: Mapped[float | None] = mapped_column(Float)
    legal_sell_value: Mapped[float | None] = mapped_column(Float)
    legal_sell_count: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32), default="tsetmc")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class IntradaySnapshot(Base):
    """Market-watch snapshot of one instrument at one moment of the session."""

    __tablename__ = "intraday_snapshots"

    ins_code: Mapped[str] = mapped_column(String(24), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True)  # our capture time (Tehran)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    tsetmc_time: Mapped[int | None] = mapped_column(Integer)  # hEven of last update
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    last: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    yesterday: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)  # cumulative for the day
    value: Mapped[float | None] = mapped_column(Float)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    price_limit_high: Mapped[float | None] = mapped_column(Float)
    price_limit_low: Mapped[float | None] = mapped_column(Float)
    # client type (cumulative volumes/counts for the day; values are derived, see data_quality)
    real_buy_volume: Mapped[float | None] = mapped_column(Float)
    real_sell_volume: Mapped[float | None] = mapped_column(Float)
    legal_buy_volume: Mapped[float | None] = mapped_column(Float)
    legal_sell_volume: Mapped[float | None] = mapped_column(Float)
    real_buy_count: Mapped[int | None] = mapped_column(Integer)
    real_sell_count: Mapped[int | None] = mapped_column(Integer)
    legal_buy_count: Mapped[int | None] = mapped_column(Integer)
    legal_sell_count: Mapped[int | None] = mapped_column(Integer)
    # order book: 5 levels, lists ordered best -> worst
    bid_prices: Mapped[list | None] = mapped_column(JSON)
    bid_volumes: Mapped[list | None] = mapped_column(JSON)
    bid_counts: Mapped[list | None] = mapped_column(JSON)
    ask_prices: Mapped[list | None] = mapped_column(JSON)
    ask_volumes: Mapped[list | None] = mapped_column(JSON)
    ask_counts: Mapped[list | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default="tsetmc")
    data_quality: Mapped[str] = mapped_column(String(16), default="ok")


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default="tsetmc")


class CodalLetter(Base):
    __tablename__ = "codal_letters"

    tracing_no: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    company_name: Mapped[str | None] = mapped_column(String(255))
    ins_code: Mapped[str | None] = mapped_column(String(24), index=True)
    letter_code: Mapped[str | None] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(Text)
    report_type: Mapped[str] = mapped_column(String(48), index=True)
    period_end: Mapped[date | None] = mapped_column(Date)
    period_months: Mapped[int | None] = mapped_column(Integer)
    audited: Mapped[bool | None] = mapped_column(Boolean)
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    url: Mapped[str | None] = mapped_column(Text)
    attachment_url: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    excel_url: Mapped[str | None] = mapped_column(Text)
    event_tags: Mapped[list | None] = mapped_column(JSON)
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|ok|error|skip
    parse_error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="codal")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class FinancialStatement(Base):
    __tablename__ = "financial_statements"

    tracing_no: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sheet: Mapped[str] = mapped_column(String(32), primary_key=True)  # balance_sheet|income|cash_flow|monthly_sales
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    period_end: Mapped[date | None] = mapped_column(Date, index=True)
    fiscal_year_end: Mapped[date | None] = mapped_column(Date)
    period_months: Mapped[int | None] = mapped_column(Integer)
    audited: Mapped[bool | None] = mapped_column(Boolean)
    consolidated: Mapped[bool | None] = mapped_column(Boolean)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    raw_data: Mapped[dict | None] = mapped_column(JSON)
    normalized_data: Mapped[dict | None] = mapped_column(JSON)
    data_quality: Mapped[str] = mapped_column(String(16), default="ok")
    source: Mapped[str] = mapped_column(String(32), default="codal")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class FeatureSnapshot(Base):
    __tablename__ = "feature_snapshots"

    ins_code: Mapped[str] = mapped_column(String(24), primary_key=True)
    asof: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)  # tablo|technical|fundamental|timeseries
    features: Mapped[dict] = mapped_column(JSON)
    data_quality: Mapped[dict | None] = mapped_column(JSON)


class Analysis(Base):
    __tablename__ = "analyses"

    ins_code: Mapped[str] = mapped_column(String(24), primary_key=True)
    asof: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), primary_key=True)  # short|swing|fundamental
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(24))
    scores: Mapped[dict] = mapped_column(JSON)
    probability: Mapped[dict | None] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)
    narrative: Mapped[str] = mapped_column(Text)
    risks: Mapped[list | None] = mapped_column(JSON)
    scan_tags: Mapped[list | None] = mapped_column(JSON)
    data_time: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)


class ModelRegistry(Base):
    __tablename__ = "model_registry"

    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    train_start: Mapped[date | None] = mapped_column(Date)
    train_end: Mapped[date | None] = mapped_column(Date)
    test_start: Mapped[date | None] = mapped_column(Date)
    test_end: Mapped[date | None] = mapped_column(Date)
    features: Mapped[list] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)
    reliability: Mapped[list | None] = mapped_column(JSON)
    calibrated: Mapped[bool] = mapped_column(Boolean, default=False)
    path: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    name: Mapped[str] = mapped_column(String(128))
    conditions: Mapped[dict] = mapped_column(JSON)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    results: Mapped[dict] = mapped_column(JSON)


class FetchLog(Base):
    __tablename__ = "fetch_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(128))
    ok: Mapped[bool] = mapped_column(Boolean)
    rows: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)


Index("ix_daily_prices_date", DailyPrice.date)
Index("ix_snap_code_date", IntradaySnapshot.ins_code, IntradaySnapshot.trade_date)
