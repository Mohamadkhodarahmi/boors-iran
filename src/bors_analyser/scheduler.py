"""Job schedule (Tehran time). Every job is isolated: an exception is logged and the scheduler goes on."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from bors_analyser.config import PROJECT_ROOT, get_settings
from bors_analyser.db.session import init_db
from bors_analyser.timeutil import TEHRAN, in_session, is_trading_day, now_tehran

log = logging.getLogger(__name__)
TRADING_DAYS = "sat,sun,mon,tue,wed"
_lock = threading.Lock()  # heavy jobs never overlap


def _job(name: str, heavy: bool = False) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def deco(fn: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            if heavy and not _lock.acquire(blocking=False):
                log.info("job %s skipped: another heavy job is running", name)
                return
            try:
                log.info("job %s started", name)
                fn()
                log.info("job %s finished", name)
            except Exception:
                log.exception("job %s failed", name)
            finally:
                if heavy:
                    _lock.release()
        run.__name__ = run.__qualname__ = name
        return run
    return deco


def _publish_if_configured(evs, scan=None) -> None:
    s = get_settings()
    if not (s.worker_url and s.ingest_token):
        log.info("publish skipped (BORS_WORKER_URL / BORS_INGEST_TOKEN not set)")
        return
    from bors_analyser.publisher import publish

    publish(evs, scan)


@_job("snapshot")
def job_snapshot() -> None:
    s = get_settings()
    if not in_session(start=s.session_start, end=s.session_end):
        return
    from bors_analyser.collectors.tsetmc import collect_snapshot

    asyncio.run(collect_snapshot())


@_job("intraday_analysis", heavy=True)
def job_intraday_analysis() -> None:
    s = get_settings()
    if not in_session(start="09:05", end=s.session_end):
        return
    from bors_analyser.analysis.engine import analyze

    evs = analyze(modes=("short",), with_fundamentals=False)
    _publish_if_configured(evs)


@_job("end_of_day", heavy=True)
def job_end_of_day() -> None:
    from bors_analyser.analysis.engine import analyze
    from bors_analyser.collectors.tsetmc import collect_snapshot, finalize_day
    from bors_analyser.scanner import scan

    asyncio.run(collect_snapshot())  # final state after the close
    finalize_day()
    evs = analyze(modes=("short", "swing", "fundamental"))
    result = scan(evs=evs)
    (PROJECT_ROOT / "data" / "latest_scan.json").write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    _publish_if_configured(evs, result)


@_job("history_update", heavy=True)
def job_history_update() -> None:
    """Official daily rows replace provisional ones once TSETMC archives the day."""
    from bors_analyser.__main__ import _instruments_for
    from bors_analyser.collectors.tsetmc import backfill

    asyncio.run(backfill(_instruments_for(None)))


@_job("codal_poll")
def job_codal_poll() -> None:
    from bors_analyser.collectors.codal import parse_pending, poll_letters

    # pages until it reaches known letters: busy hours and PC downtime are caught up
    asyncio.run(poll_letters(pages=40, stop_when_known=True))
    asyncio.run(parse_pending(limit=80))


@_job("setups", heavy=True)
def job_setups() -> None:
    from bors_analyser.analysis.engine import run_setup_studies

    run_setup_studies(start=date(2018, 1, 1), eval_start=date(2019, 1, 1))


@_job("train", heavy=True)
def job_train() -> None:
    from bors_analyser.__main__ import main

    main(["train"])


def _last_expected_close(now: datetime) -> date:
    """Most recent trading day whose official daily data should exist by now (published ~19:00)."""
    d = now.date() if now.time() >= time(19, 0) else now.date() - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


@_job("catch_up")
def job_catch_up() -> None:
    """Run at start-up: redo what was missed while the PC was off (missed cron runs are not replayed)."""
    from sqlalchemy import func, select

    from bors_analyser.db.models import Analysis, DailyPrice
    from bors_analyser.db.session import session_scope

    now = now_tehran().replace(tzinfo=None)
    with session_scope() as s:
        last_analysis = s.execute(select(func.max(Analysis.asof))).scalar()
        last_official = s.execute(select(func.max(DailyPrice.date)).where(DailyPrice.source == "tsetmc")).scalar()
    if last_official is None or last_official < _last_expected_close(now):
        log.info("catch-up: official history ends %s -> history update", last_official)
        job_history_update()
    if is_trading_day(now.date()) and now.time() >= time(13, 20) and (
            last_analysis is None or last_analysis.date() < now.date()):
        log.info("catch-up: today's end-of-day analysis missing -> running it")
        job_end_of_day()


def build_scheduler() -> BlockingScheduler:
    s = get_settings()
    sched = BlockingScheduler(timezone=TEHRAN, job_defaults={"coalesce": True, "max_instances": 1,
                                                             "misfire_grace_time": 120})
    sched.add_job(job_snapshot, CronTrigger(day_of_week=TRADING_DAYS, hour="8-12",
                                            minute=f"*/{s.snapshot_interval_min}", timezone=TEHRAN))
    sched.add_job(job_intraday_analysis, CronTrigger(day_of_week=TRADING_DAYS, hour="9-12", minute="*/10",
                                                     timezone=TEHRAN))
    sched.add_job(job_end_of_day, CronTrigger(day_of_week=TRADING_DAYS, hour=13, minute=20, timezone=TEHRAN))
    sched.add_job(job_history_update, CronTrigger(day_of_week=TRADING_DAYS, hour=19, minute=0, timezone=TEHRAN))
    sched.add_job(job_codal_poll, CronTrigger(hour="7-23", minute="*/15", timezone=TEHRAN))
    sched.add_job(job_setups, CronTrigger(day_of_week=TRADING_DAYS, hour=20, minute=0, timezone=TEHRAN))
    sched.add_job(job_train, CronTrigger(day_of_week="thu", hour=10, minute=0, timezone=TEHRAN))
    return sched


def run(with_api: bool = True) -> None:
    init_db()
    s = get_settings()
    if with_api:
        import uvicorn

        threading.Thread(
            target=uvicorn.run, args=("bors_analyser.api.app:app",),
            kwargs={"host": s.api_host, "port": s.api_port, "log_level": "warning"}, daemon=True,
        ).start()
        log.info("local API on http://%s:%d", s.api_host, s.api_port)
    sched = build_scheduler()
    start = datetime.now(TEHRAN)
    sched.add_job(job_codal_poll, next_run_time=start + timedelta(seconds=10), id="startup_codal")
    sched.add_job(job_catch_up, next_run_time=start + timedelta(seconds=30), id="startup_catch_up")
    for job in sched.get_jobs():
        log.info("scheduled %-22s next: %s", job.name, job.trigger)
    log.info("scheduler started at %s (Tehran)", now_tehran() + timedelta(0))
    sched.start()
