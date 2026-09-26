# Iranian Market Intelligence Engine — Architecture

Status: living document. Updated at the end of every phase (see `docs/PHASE_LOG.md`).

## 1. Goals and non-goals

The system answers, per symbol and for the whole market:

1. What happened? (observed data — tablo, money flow, price, disclosures)
2. Why might it have happened? (model interpretation — clearly labelled as interpretation)
3. Is the fundamental picture aligned with market behaviour? (divergence)
4. What happened historically in similar setups? (backtest evidence)
5. What is the main risk?

It is **not** a signal-selling bot. Hard rules enforced in code:

- Every output separates `observed` facts from `interpretation`.
- A number is only called a *probability* if it comes from a calibrated model with a
  recorded backtest (`model_registry.calibrated = true`). Otherwise it is a **Model Score** (0–100)
  and no percentage is shown.
- Banned vocabulary (`BUY`, `SELL`, `قطعی`, `تضمین`, `بدون ریسک`, `حتماً`, `100%` …) is rejected by a
  guard in the analysis layer; allowed labels are `Positive setup / Candidate / Watch / Neutral /
  Risky / Negative`.
- Missing data is stored and shown as `data unavailable` — never imputed silently.

## 2. Runtime topology

```
 ┌──────────── Local Windows PC (later: Iranian VPS, same code) ─────────────┐
 │                                                                            │
 │  TSETMC (cdn.tsetmc.com)   CODAL (search.codal.ir, www.codal.ir)           │
 │        │  direct, NO proxy          │  direct, NO proxy                    │
 │        ▼                            ▼                                      │
 │  collectors/tsetmc.py       collectors/codal.py                            │
 │        │                            │                                      │
 │        └────────────► DB (SQLite MVP → PostgreSQL) ◄───────┘               │
 │                           │                                                │
 │   features/tablo.py  features/timeseries.py  features/technical.py         │
 │   features/fundamental.py                                                  │
 │                           ▼                                                │
 │   decision/ (rule scores + LightGBM + calibration)  backtest/              │
 │                           ▼                                                │
 │   analysis/ (template narrator, divergence, guardrails)   scanner.py       │
 │                           ▼                                                │
 │   api/ (FastAPI, local dashboard API)   publisher.py ──────────┐           │
 │   scheduler (APScheduler) drives everything                     │           │
 └─────────────────────────────────────────────────────────────────┼──────────┘
                                                   via HTTPS proxy │ Bearer token
                                                                   ▼
                        Cloudflare Worker  (POST /api/ingest/*, Telegram webhook)
                                   │            D1 (latest analysis, users, signals)
                                   ▼
                              Telegram Bot
```

### Network constraint (verified 2026-09-23)

The PC uses a system proxy `127.0.0.1:2081` that exits abroad. TSETMC and CODAL **block foreign
IPs** (requests through the proxy time out), while Telegram/Cloudflare **need** the proxy.
`net.py` therefore builds two kinds of clients:

| Target | Proxy | How |
|---|---|---|
| `*.tsetmc.com`, `*.codal.ir`, `*.tse.ir` | never | `trust_env=False` (httpx) / aiohttp default |
| Cloudflare Worker, Telegram | `OUTBOUND_PROXY` setting (default: env) | explicit `proxy=` |

On an Iranian VPS the same config works with `OUTBOUND_PROXY` empty.

## 3. Data-source package evaluation

Evaluated 2026-09-23 on this machine (Python 3.14, Windows 10) against live data.

| Package | Last release | Verdict | Reason |
|---|---|---|---|
| **5j9/tsetmc** (`tsetmc` 5.1.0) | 2026-08 | **Adopted** — primary TSETMC client | Actively maintained, async (aiohttp), Polars output. Live-tested: `market_watch_init` (all ~3.8k instruments + 5-level best limits), `client_type_all`, `Instrument.info / closing_price_info / daily_closing_price / client_type_history / best_limits / price_history(adjusted=True) / codal`. Requires Python ≥ 3.14 → project pinned to 3.14 via uv. |
| mahs4d/tsetmc-api 5.3.0 | 2023-06 | Rejected | Unmaintained 3 years, pins pydantic v1 and lxml<5. |
| BabakEslami/tse-market-data | 2026-07 | **Used as reference** | Not a library: a verified endpoint catalogue (cdn.tsetmc.com + webgw.tse.ir). Used to understand endpoint quirks (User-Agent required, `GetTradeHistory` under-counts volume by ~36%, BestLimits history is a delta feed, halted symbols return empty). |
| **yghaderi/codalpy** 0.4.5 | 2025-09 | **Partially adopted** — only `codalpy.models.DataSource` for parsing the `var datasource = …` JSON embedded in CODAL report pages | Its `Codal` class is unusable for whole-market work: hard-coded issuer whitelist, sync `requests` that inherits the geo-blocked proxy, fixed 2 s/10 s timeouts with no retry, errors are `print`ed. The DataSource model parsed current (1405) income statements and balance sheets correctly. |
| amirziveh/codal-skill | 2026-09 | **Used as reference** | Reverse-engineered CODAL search API catalogue + letter-type taxonomy. Our CODAL client follows it. |
| mohsenebrahimyir/codal-tsetmc 3.1.1 | 2025-09 | Rejected | Full framework with its own DB layer, pandas<3, lxml<6; overlaps our architecture. |
| mohapak/codal_scraper | 2025-12 | Rejected | Board-of-directors scraping only. |

Consequence: **TSETMC** = `tsetmc` library wrapped by an adapter (`collectors/tsetmc.py`) so it can be
swapped. **CODAL** = thin own async client for `search.codal.ir/api/search/v2/q` + codalpy's
`DataSource` model for statement parsing. Known CODAL pitfall: a symbol search also returns
subsidiaries' letters (e.g. «فولاد» returned the Sepahan sports club's statements) → we filter with
`Mains=true, Childs=false` and verify `CompanyName`.

## 4. Local package layout

```
src/bors_analyser/
  __main__.py          CLI:  python -m bors_analyser <run|collect|backfill|codal|features|backtest|train|analyze|scan|publish|api>
  config.py            pydantic-settings; reads .env + config/*.yaml
  logging_setup.py     rotating file + console logs (logs/app.log)
  net.py               proxy-aware HTTP client factories
  timeutil.py          Tehran timezone, Jalali <-> Gregorian, trading session helpers
  db/                  SQLAlchemy 2.0 models, engine/session, upsert helpers
  collectors/          tsetmc.py, codal.py   (fetch + normalize, never compute features)
  features/            tablo.py, timeseries.py, technical.py, fundamental.py (pure functions → testable)
  decision/            scoring.py (config-driven rule scores), model.py (LightGBM), calibration.py
  backtest/            labels.py (forward returns), engine.py (condition study, walk-forward)
  analysis/            narrator.py (Persian template text), divergence.py, guard.py
  scanner.py           market-wide categorisation
  publisher.py         POST results to the Cloudflare Worker
  api/                 FastAPI app for a future dashboard
  scheduler.py         APScheduler jobs
config/
  settings.example.env
  scoring.yaml         ALL weights/thresholds (nothing hard-coded)
worker/                Cloudflare Worker (TypeScript) + D1 schema
tests/                 unit tests on fixtures + opt-in live tests (-m live)
start.bat              one-command start on Windows
```

## 5. Database schema (local)

SQLAlchemy Core/ORM, dialect-neutral types. SQLite by default
(`sqlite:///data/bors.db`), PostgreSQL by setting `DATABASE_URL=postgresql+psycopg://…`.
All money in **Rial**, volumes in shares. Every data row carries `source`, `fetched_at` and, where
applicable, `data_quality` (`ok | partial | stale | unavailable`).

| Table | Key | Purpose |
|---|---|---|
| `instruments` | `ins_code` | symbol (l18), name (l30), isin, market/flow, board (cgrValCot), sector code+name, is_stock flag, base_volume, total_shares |
| `daily_prices` | `(ins_code, date)` | open/high/low/close(pc)/last(pl)/yesterday, volume, value, trade_count; raw **and** adjusted close |
| `daily_client_type` | `(ins_code, date)` | حقیقی/حقوقی buy/sell volume, value, count |
| `intraday_snapshots` | `(ins_code, ts)` | periodic market-watch snapshot during session: prices, cumulative vol/val/count, client type, 5-level bid/ask price/vol/count, buyer/seller counts |
| `codal_letters` | `tracing_no` | every disclosure: symbol, company, letter_code, title, sent/publish time, urls, `report_type` (normalized), `event_tags` |
| `financial_statements` | `(tracing_no, sheet)` | period_end, fiscal_year_end, period_months, audited, consolidated, `raw_data` (JSON), `normalized_data` (JSON of canonical items) |
| `monthly_activity` | `(tracing_no)` | normalized monthly sales (period, revenue, cumulative) + raw |
| `feature_snapshots` | `(ins_code, asof, kind)` | computed feature vectors (JSON) — reproducible inputs for the model |
| `analyses` | `(ins_code, asof, mode)` | scores, labels, narrative, risks, evidence; what gets published |
| `model_registry` | `model_id` | target definition, train/test windows, metrics, `calibrated` flag, reliability table |
| `backtest_runs` | `run_id` | condition set, sample stats (count, win rate, mean/median, 1/3/5/10d returns, MDD) |
| `fetch_log` | autoinc | source, endpoint, ok/error, duration, rows, message — data-quality audit trail |

Cloudflare D1 keeps only what the bot serves (see `worker/schema.sql`): `users`, `stocks`,
`market_snapshots` (latest), `stock_analysis` (latest per symbol+mode), `signals`, `codal_events`
(recent), `daily_summaries`, `model_predictions`, `rate_limits`.

## 6. Feature engines

All feature functions are pure (`DataFrame in → dict/DataFrame out`), computed **only from data with
timestamp ≤ as-of time** (look-ahead guard is tested).

- **Tablo** (`features/tablo.py`): volume ratios 5/20/30d, relative volume (intraday-time-adjusted),
  real money flow = حقیقی buy value − حقیقی sell value, ratio to traded value, cumulative 5/20d,
  per-capita buy/sell (buyer power), order-book imbalance / spread / queue strength / concentration,
  price behaviour (intraday return, close vs last, distance to high/low, gap, momentum), trade
  behaviour (avg trade size, trade-count anomaly, unusual-activity z-scores).
- **Time-series** (`features/timeseries.py`): from intraday snapshots, per-metric slope, Δ over
  5m/15m/30m, regime label (`rising / falling / flat / jump`) using configurable thresholds.
- **Technical** (`features/technical.py`): SMA/EMA, RSI, MACD, ATR, Bollinger, pivot-based
  support/resistance, breakout, trend, realised volatility — on **adjusted** prices.
- **Fundamental** (`features/fundamental.py`): from normalized statements: revenue/profit/EPS growth
  (YoY same-period), gross/operating/net margin, debt growth, monthly & quarterly sales growth, event
  flags (capital increase, major contract, auditor issue, major disclosure, recent report ±).

## 7. Decision layer

Two independent layers, both required:

1. **Rule-based component scores** (`decision/scoring.py`) — `tablo_score`, `technical_score`,
   `fundamental_score`, `market_score`, `risk_score`, `overall_score` in 0–100. Every weight and
   threshold lives in `config/scoring.yaml`. These are *descriptive* scores, labelled "Model Score",
   never probabilities. Backtests are used to tune the YAML, not the other way around.
2. **Statistical model** (`decision/model.py`) — **LightGBM** binary classifier per target, e.g.
   `T1: P(max high within 3 trading days ≥ +3%)`, `T2: P(close-to-close return over 5 days > +5%)`.
   - Walk-forward / time-based split, purged by the label horizon (no random split).
   - Calibration with isotonic regression on a held-out *later* window; reliability table + Brier
     score stored in `model_registry`.
   - Only when `calibrated=true` and test sample size ≥ config minimum does the bot show
     `Probability: 72% (historical calibrated accuracy ≈ X%)`.

Why LightGBM (vs XGBoost / CatBoost / temporal nets): tabular, heterogeneous, missing-value-heavy
features on a small universe (~700 stocks × ~2–10 years daily); GBDTs are the strongest baseline
there, train in seconds on CPU, handle NaN natively, and give feature importance for the narrative.
XGBoost is equivalent (kept as an option); CatBoost's benefit (categorical handling) is small here;
deep temporal models (TFT, LSTM) need more data than one market provides and are harder to calibrate.

### JEV (TypeSafe "System One" decision model)

Assessed 2026-09 from TypeSafe's public material:

| Question | Answer |
|---|---|
| Input | Text/program state (not numeric tensors); structured context serialized as text |
| Output | Predefined categorical/boolean/rating values with confidence scores (≤255 choices) |
| Real probability? | Calibrated on TypeSafe's verifiable-reward tasks (RLCD). **Not** calibrated on TSE forward returns — its confidence would need our own reliability test before being shown. |
| Fine-tunable | Not documented |
| Runs locally? | No — hosted API only, early access / waitlist |
| Hardware | N/A (remote); requires outbound proxy from Iran |
| Latency | 70–500 ms per call (vendor figure) |

Decision: JEV is **not** the price-prediction engine (not trained on our numeric data, not
backtestable offline, API dependency). It is a good candidate for a narrower task where text is the
input: **classifying CODAL disclosures** into event types / sentiment with confidence.
**Status: not integrated.** Disclosures are currently classified by title rules
(`collectors/codal.py: classify_letter`). A JEV provider is a planned option: it needs an API key
(early access) and must be evaluated against a hand-labelled set before its output is trusted.

## 8. Analysis layer (human-like text)

`analysis/narrator.py` builds Persian text from **templates bound to input fields**: every sentence
is emitted only if its source field exists and passes a threshold, so the narrative cannot mention
data that is not in the input. An LLM may later rephrase the text, but only with the evidence JSON
as context and the guard re-checking numbers; the LLM never produces scores.

Divergence (`analysis/divergence.py`) maps component directions (+/0/−) to wording such as
"هم‌جهتی قوی", "واگرایی بازار/بنیاد", "کاندید احتمالی انباشت" — always phrased as possibility.

Modes: `short` (tablo + intraday), `swing` (tablo + technical + fundamental + history),
`fundamental` (CODAL-only view).

## 9. Backtesting

`backtest/engine.py`:
- Condition study: "when `volume_ratio_20 > 3 & buyer_power > 2 & …` was true at close of day t,
  what were returns from t+1 open / t close over 1/3/5/10 days?" → sample_count, win_rate,
  mean/median, max gain/loss, max adverse excursion.
- Entry is at **next** trading day's first price (you cannot trade at the close you observed).
- Price-limit (دامنه نوسان) awareness: days where the stock opened at the limit are flagged,
  since fills are unrealistic.
- Adjusted prices for returns (capital increases / dividends).

## 10. Cloudflare Worker + Telegram

`worker/` (TypeScript, Wrangler 4, D1):
- `POST /api/ingest/{analysis|market|signals|codal}` — `Authorization: Bearer INGEST_TOKEN`,
  JSON schema validation, upsert into D1.
- `POST /telegram/webhook` — verified with `X-Telegram-Bot-Api-Secret-Token`.
- Commands `/start /help /market /top /stock /scan /fundamental /tablo /technical`.
- Per-user rate limit (D1 counter per minute), admin allow-list, user registry.
- Read-only `GET` endpoints mirror the local API for a future dashboard.
- Secrets only in `wrangler secret`, never in code or messages.

## 11. Scheduling (Tehran time, Sat–Wed)

| Job | When |
|---|---|
| intraday snapshot (market watch + client type) | every 2 min 08:55–12:35 (configurable) |
| intraday analysis + publish (short mode) | every 10 min during session |
| end-of-day daily history update | 13:30 |
| CODAL letters poll | every 15 min 07:00–23:00 |
| statements / monthly activity parse for new letters | after poll |
| full analysis (swing / fundamental) + scan + publish | 14:00 |
| model retrain + calibration | weekly (Thursday) |

Failures of one symbol are logged to `fetch_log` and skipped; jobs never abort the scheduler.

## 12. Moving to a VPS

Nothing is Windows-specific except `start.bat`. On a Linux VPS in Iran: set `DATABASE_URL` to
PostgreSQL, clear `OUTBOUND_PROXY` (or keep one if Telegram/Cloudflare are filtered), run
`python -m bors_analyser run` under systemd. The Worker is unchanged.
