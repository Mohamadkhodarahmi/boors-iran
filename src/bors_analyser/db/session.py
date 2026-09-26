"""Engine/session management, dialect-aware upserts and the fetch audit log."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from bors_analyser.config import get_settings
from bors_analyser.db.models import Base, FetchLog
from bors_analyser.timeutil import now_tehran

log = logging.getLogger(__name__)


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # scheduler writes while the API reads
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


def init_db(engine: Engine | None = None) -> Engine:
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    return engine


@lru_cache
def _session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False) if engine else _session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _insert_for(session: Session):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise NotImplementedError(f"upsert not implemented for {dialect}")
    return insert


def upsert(
    session: Session,
    model: type[Base],
    rows: Iterable[dict[str, Any]],
    *,
    update_columns: Iterable[str] | None = None,
    chunk_size: int = 500,
) -> int:
    """INSERT … ON CONFLICT(pk) DO UPDATE. Returns number of rows sent."""
    rows = list(rows)
    if not rows:
        return 0
    insert = _insert_for(session)
    table = model.__table__
    pk = [c.name for c in table.primary_key.columns]
    columns = {c.name for c in table.columns}
    if update_columns is None:
        update_columns = [c for c in columns if c not in pk]
    # SQLite limits bound parameters per statement; keep chunks small enough.
    per_row = max(1, len(rows[0]))
    chunk_size = max(1, min(chunk_size, 30000 // per_row))
    for i in range(0, len(rows), chunk_size):
        chunk = [{k: v for k, v in r.items() if k in columns} for r in rows[i : i + chunk_size]]
        stmt = insert(table).values(chunk)
        cols = [c for c in update_columns if c in chunk[0]]
        if cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=pk, set_={c: stmt.excluded[c] for c in cols}
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk)
        session.execute(stmt)
    return len(rows)


class FetchAudit:
    """Times an operation and records success/failure in `fetch_log` without raising."""

    def __init__(self, source: str, operation: str, target: str | None = None):
        self.source, self.operation, self.target = source, operation, target
        self.rows: int | None = None
        self.message: str | None = None

    def __enter__(self) -> FetchAudit:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        ok = exc is None
        msg = self.message if ok else f"{exc_type.__name__}: {exc}"
        duration = int((time.perf_counter() - self._t0) * 1000)
        level = logging.INFO if ok else logging.WARNING
        log.log(level, "%s %s %s ok=%s rows=%s %dms %s", self.source, self.operation,
                self.target or "", ok, self.rows, duration, msg or "")
        try:
            with session_scope() as s:
                s.add(FetchLog(ts=now_tehran().replace(tzinfo=None), source=self.source,
                               operation=self.operation, target=self.target, ok=ok,
                               rows=self.rows, duration_ms=duration, message=(msg or "")[:2000]))
        except Exception:  # audit must never break the pipeline
            log.exception("failed to write fetch_log")
        return False  # never swallow; callers decide
