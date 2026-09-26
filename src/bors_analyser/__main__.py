"""Command-line entry point: `python -m bors_analyser <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

from bors_analyser.db.models import Instrument
from bors_analyser.db.session import init_db, session_scope
from bors_analyser.logging_setup import setup_logging
from bors_analyser.timeutil import normalize_fa

log = logging.getLogger("bors_analyser")


def _instruments_for(symbols: list[str] | None, stocks_only: bool = True) -> list[tuple[str, str]]:
    with session_scope() as s:
        q = select(Instrument.ins_code, Instrument.symbol)
        if symbols:
            q = q.where(Instrument.symbol.in_([normalize_fa(x) for x in symbols]))
        elif stocks_only:
            q = q.where(Instrument.is_stock.is_(True))
        rows = [(c, sym) for c, sym in s.execute(q).all()]
    if symbols:
        found = {sym for _, sym in rows}
        missing = [x for x in symbols if normalize_fa(x) not in found]
        if missing:
            log.warning("symbols not in instruments table (run `snapshot` first?): %s", missing)
    return rows


def cmd_init_db(_: argparse.Namespace) -> None:
    engine = init_db()
    log.info("database ready: %s", engine.url.render_as_string(hide_password=True))


def cmd_snapshot(args: argparse.Namespace) -> None:
    from bors_analyser.collectors.tsetmc import collect_snapshot

    init_db()
    asyncio.run(collect_snapshot(stocks_only=not args.all))


def cmd_backfill(args: argparse.Namespace) -> None:
    from bors_analyser.collectors.tsetmc import backfill, collect_snapshot

    init_db()
    targets = _instruments_for(args.symbols)
    if not targets:
        asyncio.run(collect_snapshot())
        targets = _instruments_for(args.symbols)
    if args.limit:
        targets = targets[: args.limit]
    log.info("backfilling %d instruments", len(targets))
    result = asyncio.run(backfill(targets))
    log.info("backfill done: ok=%d failed=%d", result["ok"], len(result["failed"]))


def cmd_finalize_day(_: argparse.Namespace) -> None:
    from bors_analyser.collectors.tsetmc import finalize_day

    init_db()
    finalize_day()


def cmd_codal_poll(args: argparse.Namespace) -> None:
    from bors_analyser.collectors.codal import poll_letters

    init_db()
    asyncio.run(poll_letters(pages=args.pages, symbol=args.symbol))


def cmd_codal_parse(args: argparse.Namespace) -> None:
    from bors_analyser.collectors.codal import parse_pending

    init_db()
    asyncio.run(parse_pending(limit=args.limit, symbol=args.symbol))


def cmd_codal_sync(args: argparse.Namespace) -> None:
    """Letters for the given symbols since `--from` (Jalali) and parse their statements."""
    from bors_analyser.collectors.codal import parse_pending, poll_letters

    init_db()

    async def run() -> None:
        for sym in args.symbols:
            try:
                await poll_letters(pages=args.pages, symbol=sym, from_jdate=args.from_jdate)
                await parse_pending(limit=500, symbol=sym)
            except Exception:
                log.exception("codal sync failed for %s", sym)

    asyncio.run(run())


def cmd_codal_bootstrap(args: argparse.Namespace) -> None:
    from bors_analyser.collectors.codal import bootstrap, parse_pending, reclassify_all

    init_db()
    reclassify_all()
    if not args.parse_only:
        asyncio.run(bootstrap(args.monthly_from, args.statements_from))
    asyncio.run(parse_pending(limit=args.limit, concurrency=args.concurrency))


def cmd_backtest(args: argparse.Namespace) -> None:
    import json
    from datetime import date

    from bors_analyser.backtest.engine import condition_study, parse_conditions, save_run
    from bors_analyser.pipeline import build_research_panel

    init_db()
    conds = parse_conditions(args.conditions)
    panel = build_research_panel(start=date.fromisoformat(args.start))
    res = condition_study(panel, conds, min_avg_value=args.min_value, cooldown=args.cooldown,
                          start=date.fromisoformat(args.start) if args.eval_start is None
                          else date.fromisoformat(args.eval_start))
    run_id = save_run(args.name or args.conditions, res)
    res["run_id"] = run_id
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


def cmd_train(args: argparse.Namespace) -> None:
    import json
    from datetime import date

    from bors_analyser.config import get_scoring_config
    from bors_analyser.decision.model import TargetSpec, save_model, train_target
    from bors_analyser.pipeline import build_research_panel

    init_db()
    cfg = get_scoring_config()
    mcfg = cfg["model"]
    min_value = cfg["universe"]["min_avg_value_20"]
    panel = build_research_panel(start=date.fromisoformat(mcfg["history_start"]), min_avg_value=min_value)
    for t in mcfg["targets"]:
        if args.target and t["id"] != args.target:
            continue
        spec = TargetSpec(**t)
        log.info("training %s: %s", spec.id, spec.describe())
        result = train_target(panel, spec, mcfg, min_value)
        model_id = save_model(spec, result)
        m = result["metrics"]
        summary = {"model_id": model_id, "calibrated": result["calibrated"], "splits": m["splits"],
                   "test_calibrated": m["test_calibrated"], "calibration_check": m["calibration_check"],
                   "top_features": list(m["feature_importance_gain"])[:10],
                   "reliability": result["reliability"]}
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def cmd_setups(_: argparse.Namespace) -> None:
    from bors_analyser.analysis.engine import run_setup_studies

    init_db()
    run_setup_studies()


def cmd_analyze(args: argparse.Namespace) -> None:
    from bors_analyser.analysis.engine import analyze

    init_db()
    modes = tuple(args.mode.split(","))
    results = analyze(args.symbols or None, modes=modes, with_fundamentals=not args.no_fundamentals)
    if args.print:
        for ev in results:
            print(ev["telegram_text"])
            print()


def cmd_scan(args: argparse.Namespace) -> None:
    import json

    from bors_analyser.scanner import scan, scan_text

    init_db()
    result = scan()
    from bors_analyser.config import PROJECT_ROOT

    (PROJECT_ROOT / "data" / "latest_scan.json").write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    print(scan_text(result) if not args.json else json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_publish(args: argparse.Namespace) -> None:
    from bors_analyser.analysis.engine import analyze
    from bors_analyser.publisher import publish
    from bors_analyser.scanner import scan

    init_db()
    evs = analyze(args.symbols or None, modes=("short", "swing", "fundamental"))
    result = scan(evs=evs) if not args.symbols else None
    publish(evs, result)


def cmd_run(args: argparse.Namespace) -> None:
    from bors_analyser.scheduler import run

    run(with_api=not args.no_api)


def cmd_api(_: argparse.Namespace) -> None:
    import uvicorn

    from bors_analyser.config import get_settings

    s = get_settings()
    uvicorn.run("bors_analyser.api.app:app", host=s.api_host, port=s.api_port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bors_analyser", description="Iranian Market Intelligence Engine")
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create database tables").set_defaults(func=cmd_init_db)

    sp = sub.add_parser("snapshot", help="collect one market-watch snapshot")
    sp.add_argument("--all", action="store_true", help="store snapshots for all instruments, not only stocks")
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser("backfill", help="daily price + client-type history")
    sp.add_argument("symbols", nargs="*", help="symbols (default: all stocks)")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_backfill)

    sub.add_parser("finalize-day", help="provisional daily rows for today from last snapshot").set_defaults(
        func=cmd_finalize_day
    )

    sp = sub.add_parser("codal-poll", help="fetch recent CODAL letters")
    sp.add_argument("--pages", type=int, default=3)
    sp.add_argument("--symbol", default=None)
    sp.set_defaults(func=cmd_codal_poll)

    sp = sub.add_parser("codal-parse", help="parse pending statements / monthly reports")
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--symbol", default=None)
    sp.set_defaults(func=cmd_codal_parse)

    sp = sub.add_parser("codal-sync", help="letters + statements for specific symbols")
    sp.add_argument("symbols", nargs="+")
    sp.add_argument("--pages", type=int, default=10)
    sp.add_argument("--from", dest="from_jdate", default="1403/01/01")
    sp.set_defaults(func=cmd_codal_sync)

    sp = sub.add_parser("codal-bootstrap", help="market-wide initial CODAL load (long-running)")
    sp.add_argument("--monthly-from", default="1404/05/01", help="Jalali date; 13+ months for YoY")
    sp.add_argument("--statements-from", default="1404/01/01")
    sp.add_argument("--limit", type=int, default=100_000)
    sp.add_argument("--concurrency", type=int, default=4)
    sp.add_argument("--parse-only", action="store_true")
    sp.set_defaults(func=cmd_codal_bootstrap)

    sp = sub.add_parser("backtest", help="condition study, e.g. \"volume_ratio_20>3, buyer_power>2\"")
    sp.add_argument("conditions")
    sp.add_argument("--start", default="2018-01-01", help="history loaded from (for rolling windows)")
    sp.add_argument("--eval-start", default=None, help="first signal date (default: --start)")
    sp.add_argument("--min-value", type=float, default=5e9, help="min 20d avg traded value (Rial)")
    sp.add_argument("--cooldown", type=int, default=5)
    sp.add_argument("--name", default=None)
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("train", help="train + calibrate + evaluate LightGBM models (walk-forward)")
    sp.add_argument("--target", default=None, help="only this target id")
    sp.set_defaults(func=cmd_train)

    sub.add_parser("setups", help="backtest the named setups in config (history evidence)").set_defaults(
        func=cmd_setups)

    sp = sub.add_parser("analyze", help="full analysis for symbols (default: all stocks)")
    sp.add_argument("symbols", nargs="*")
    sp.add_argument("--mode", default="short,swing,fundamental")
    sp.add_argument("--print", action="store_true")
    sp.add_argument("--no-fundamentals", action="store_true")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("scan", help="analyse all stocks and categorise them")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("publish", help="analyse (+scan) and push results to the Cloudflare Worker")
    sp.add_argument("symbols", nargs="*")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("run", help="start scheduler (+ local API): the whole local system")
    sp.add_argument("--no-api", action="store_true")
    sp.set_defaults(func=cmd_run)

    sub.add_parser("api", help="local read-only API only").set_defaults(func=cmd_api)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
