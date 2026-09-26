# Phase log

Each phase ends with: Implemented / Tested / Known issues / Next phase.

## Phase 0 — Repository survey & decisions (2026-09-23)

- Repository was empty. Tooling: Python 3.11 system, uv 0.12.5 (installed CPython 3.14.7 for the
  project), Node 24 + wrangler 4.86, Docker installed but daemon not running, no PostgreSQL.
- **Network finding:** system proxy `127.0.0.1:2081` exits in NL; TSETMC/CODAL time out through it
  and answer in ~1 s directly. Design consequence: per-destination proxy policy (`net.py`).
- Package evaluation → see ARCHITECTURE.md §3 (tsetmc adopted; codalpy parsing model adopted;
  own CODAL search client; others rejected/reference).
- DB: SQLite for MVP (Postgres-ready via SQLAlchemy; switch with `BORS_DATABASE_URL`).

## Phase 1 — TSETMC collector

**Implemented**
- `config.py` (env + YAML), `logging_setup.py` (UTF-8 console + rotating file), `net.py` (proxy split),
  `timeutil.py` (Tehran tz, Jalali, dEven/hEven, session check).
- `db/models.py` — full local schema; `db/session.py` — engine (SQLite WAL), dialect-aware chunked
  upsert, `FetchAudit` → `fetch_log` table.
- `collectors/tsetmc.py` — market-watch snapshot (all instruments + 5-level order book + client type),
  instrument registry with sector names, full daily history (raw + adjusted close + client type with
  values), provisional end-of-day rows from the last snapshot.
- CLI: `init-db`, `snapshot`, `backfill [symbols]`, `finalize-day`.

**Tested**
- 11 unit tests (builders, time utils, upsert) + 2 live tests (`pytest -m live`).
- Live check on فولاد, فملی, خودرو, شپنا, وبملت: 3.4k–5.3k daily rows each, since 2001–2009;
  snapshot close for فولاد (3350) equals `closing_price_info`; client-type values present; order
  book 5 levels stored.

**Known issues**
- TSETMC's daily list excludes the current day until archived → `finalize-day` writes provisional rows
  (`source='tsetmc_snapshot'`, client-type values derived from VWAP), overwritten by the next backfill.
- `GetTrade` (tick trades) returns HTTP 500 after the close; tick-level features are not used yet.
- Market-watch `ClientTypeAll` gives volumes/counts only (no values) intraday.

**Next phase:** Phase 2 — full-market historical backfill + incremental daily update.

## Phase 2 — Historical database

**Implemented** full-market backfill (`backfill`), nightly `history_update` job, provisional
end-of-day rows, cleaning of feed errors (non-positive OHLC → NULL at ingestion and in panels).
**Tested** 1,137 / 1,139 stocks loaded: 1.79 M daily price rows, 1.61 M client-type rows, 2001 → 2026-09-22.
**Known issues** 2 symbols failed on transient HTTP 502 (retried by the nightly job). TSETMC reports
`open = 0` on 172 traded days; stored as missing, those days are not usable as entries.

## Phase 3 — Tablo feature engine

**Implemented** `features/tablo.py` (vectorised panel: volume ratios 5/20/30, z-score, real money flow
+ ratio + 5/20-day cumulative, buyer power + history, trade behaviour, price behaviour, momentum),
order-book features (imbalance, spread, concentration, buy/sell queue + strength), intraday relative
volume; `features/timeseries.py` (5/15/30-min changes, rising/falling/flat/jump regime).
**Tested** unit tests incl. an explicit look-ahead test (future rows do not change past features);
real panel for 1,139 stocks × 3.7 years in 0.5 s.
**Known issues** intraday regimes need several snapshots of a live session (first live session pending).

## Phase 4 — CODAL collector

**Implemented** own async client for `search.codal.ir` (Mains-only, paging, 429 back-off, request
pacer), title classifier (report type + event tags + period/audit parsing), statement normalizer
(balance sheet / income / cash flow → canonical items, current vs prior-year columns), monthly-sales
parser (production and bank templates), `codal-sync`, `codal-bootstrap`, `reclassify`.
**Tested** فولاد/خودرو/شپنا/وبملت: parsed values equal the report pages (e.g. فولاد 3M-1405 revenue
834,166,799 m-Rial, prior 847,715,717; monthly sales 544,271,929). 13 classifier tests.
**Known issues**
- CODAL's WAF throttles bursts per IP (HTTP 429 for > 45 minutes). The first market-wide bootstrap
  hit it; a pacer (1.5 s between requests) is now enforced. **The market-wide bootstrap is still to be
  run** — until then fundamentals exist for only ~5 companies.
- Many consolidated annual reports have no structured datasource → skipped (not guessed).
- Report pages sometimes take > 60 s; retried, then marked `error` and retried next run.

## Phase 5 — Fundamental engine

**Implemented** growth (YoY same period), margins, TTM profit/revenue (prior FY + YTD − prior YTD),
P/E, P/S, P/B, leverage, cash-flow trend, monthly / quarterly / YTD sales growth, event flags with an
`asof` window, report direction with an **inflation hurdle** (30 % nominal) and "loss never positive".
**Tested** 6 unit tests; live: فولاد P/E 6.7, شپنا 7.0 (hand-checked).

## Phase 6 — Technical engine

**Implemented** SMA/EMA, RSI (Wilder), MACD (+ normalized histogram), ATR, Bollinger, realised
volatility, 20/60-day support/resistance from *previous* days, breakout/breakdown, trend — on adjusted
prices. **Tested** 4 unit tests (incl. capital-increase adjustment, no cross-instrument leakage).

## Phase 7 — Backtesting

**Implemented** forward labels (entry = next day's open, adjusted; locked limit-up entries excluded),
condition studies vs. unconditional baseline, per-year stability, cooldown de-duplication, stored runs.
**Tested** 6 unit tests. Real study 2019-06 → 2026-09, "volume>3×, buyer power>2, real inflow>10 %,
momentum>0": n = 3,202; 1-day win rate 33 % vs 49 % baseline (buying the next open after such a day
tends to lose on day 1); 10-day mean +2.9 % vs +1.7 % baseline, win 57 % vs 52 %; weak in 2021–2022.

## Phase 8 — Decision model

**Implemented** config-driven component scores (`config/scoring.yaml`) with coverage, risk flags,
mode weights, labels; LightGBM per target with time-based train / calibration / test splits and
purging; isotonic calibration; reliability table; registry; `calibrated` gate.
**Tested** results (test window 2025-09 → 2026-09, ~101 k rows):

| target | AUC | top-decile hit vs base | calibrated |
|---|---|---|---|
| hit +3 % within 3 d | 0.554 | 54.4 % vs 48.5 % | **no** (bin gap 5.2 % > 5 %, Brier skill 0.007) |
| return > 5 % in 5 d | 0.561 | 35.8 % vs 27.9 % | **no** (gap 13.9 %, negative skill) |

→ The models rank weakly but their probabilities are not reliable in the latest regime; the bot shows
**Model Score only** ("Probability: در حال calibration"). JEV evaluated and not used as predictor
(see ARCHITECTURE §7).
**Known issues / next** regime-aware (rolling, shorter) calibration; fundamental features in the model
once CODAL history exists.

## Phase 9 — Human-like analysis

**Implemented** evidence assembly (daily + live overlay from snapshots), narrator with templates bound
to fields, observed vs interpretation sections, divergence detection, setup history citations,
mixed-report nuance, inflation note, language guard, Telegram card.
**Tested** 7 tests (guard, hedged wording, no invented sentences, no probability unless calibrated).
Reviewed real outputs for فولاد/خودرو/شپنا; fixed two issues found by review (low risk inflating
scores; mixed report signals).

## Phase 10/11 — Cloudflare Worker + D1 + Telegram bot

**Implemented** `worker/` (TypeScript, no runtime deps): ingest endpoints (Bearer, constant-time
compare, validation, size limits), D1 schema/migration, read API, Telegram webhook with secret header,
commands, users, admin (`/stats /approve /block /unblock`), allow-list mode, per-user rate limit,
message splitting, stale-data warning. `publisher.py` pushes from the local engine.
**Tested** type-check clean; local `wrangler dev` + local D1: 401/400/422/403 checks pass; published
3,414 analyses + market + scan + CODAL; API reads back correct data; Arabic-yeh symbol lookup works;
webhook returns 200 even when Telegram is unreachable (Telegram retries on non-2xx).
**Not done** deployment to your Cloudflare account and webhook registration (needs your credentials —
see worker/README.md). Real Telegram delivery therefore not yet verified.

## Phase 12 — Full market scanner

**Implemented** six categories, ranking, market summary, scan text, `/scan` `/top` `/market`.
**Tested** 1,139 stocks analysed, 795 liquid categorised in 14.5 s.

## Local runtime

`start.bat` → `uv sync`, `init-db`, `run` (APScheduler: snapshots every 2 min in session, intraday
analysis every 10 min, end-of-day analysis + scan + publish 13:20, official history 19:00, CODAL every
15 min, setup studies 20:00, weekly retrain Thursday). Local API on http://127.0.0.1:8008.

## Update 2026-09-23 (evening) — resumable CODAL loading, catch-up after downtime

**Implemented**
- Separate pacers: `search.codal.ir` 1 request / 5 s, `www.codal.ir` report pages 1 / s. Search pages are
  fetched sequentially and stored **page by page**; bootstrap progress is checkpointed in
  `data/codal_bootstrap_state.json`, so an interrupted run resumes where it stopped.
- The 15-minute CODAL poll pages until it reaches already-known letters (max 40 pages), so busy hours and
  PC downtime are caught up.
- Scheduler start-up catch-up: immediate CODAL poll; official history update if it is behind; today's
  end-of-day analysis if 13:20 was missed. (Intraday snapshots of hours the PC was off cannot be recovered.)

**Observed**
- search.codal.ir blocks after ~30 pages even at 1 request / 5 s, for ~35 minutes. Report pages were not
  blocked, so parsing runs in parallel with the (slow) search.
- Coverage when the session ended: 1,940 letters from 944 publishers; monthly-activity reports parsed for
  **322 companies** (517 reports); interim statements still only for 4 companies (the statement search had
  not started). Bootstrap checkpoint: monthly search at page 56.
- Monthly parser coverage: 440 / 517 reports give month sales. Not yet handled: service companies
  («خدمات و فروش», 30 reports), some bank reports (27), insurance/leasing («فرم درآمدهای عملیاتی ماهانه», 11),
  «عملکرد ماهانه» (9). Bank income statements give no EPS / operating profit under the current item map.

**Correction** ARCHITECTURE.md previously said JEV was wired behind an interface; it is not integrated.
