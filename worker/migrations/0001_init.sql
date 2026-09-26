-- D1 keeps only what the bot/API serves: latest analysis per symbol+mode, latest market state,
-- latest scan, recent CODAL events, users. Historical/raw data stays in the local database.

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,               -- Telegram user id
  username TEXT,
  first_name TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_approved INTEGER NOT NULL DEFAULT 0,
  is_blocked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen TEXT,
  request_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stocks (
  symbol_key TEXT PRIMARY KEY,          -- normalized symbol used for lookups
  symbol TEXT NOT NULL,
  name TEXT,
  sector TEXT,
  market TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_analysis (
  symbol_key TEXT NOT NULL,
  mode TEXT NOT NULL,                   -- short | swing | fundamental
  symbol TEXT NOT NULL,
  asof TEXT NOT NULL,
  data_time TEXT,
  label TEXT,
  overall_score REAL,
  risk_score REAL,
  scores TEXT,                          -- JSON
  sections TEXT,                        -- JSON (tablo/technical/fundamental/analysis/risks/model lines)
  text TEXT NOT NULL,                   -- ready Telegram card
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol_key, mode)
);
CREATE INDEX IF NOT EXISTS ix_analysis_score ON stock_analysis (mode, overall_score DESC);

CREATE TABLE IF NOT EXISTS market_snapshots (
  id TEXT PRIMARY KEY,                  -- 'latest'
  asof TEXT NOT NULL,
  data TEXT NOT NULL,                   -- JSON
  text TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY,                  -- 'latest_scan'
  asof TEXT NOT NULL,
  data TEXT NOT NULL,                   -- JSON scan result
  text TEXT NOT NULL,
  top_text TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codal_events (
  tracing_no INTEGER PRIMARY KEY,
  symbol_key TEXT,
  symbol TEXT,
  title TEXT,
  report_type TEXT,
  published_at TEXT,
  url TEXT
);
CREATE INDEX IF NOT EXISTS ix_codal_symbol ON codal_events (symbol_key, published_at DESC);

CREATE TABLE IF NOT EXISTS daily_summaries (
  date TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  data TEXT
);

CREATE TABLE IF NOT EXISTS model_predictions (
  symbol_key TEXT NOT NULL,
  target TEXT NOT NULL,
  asof TEXT NOT NULL,
  model_score REAL,
  probability REAL,                     -- NULL unless the model is calibrated
  calibrated INTEGER NOT NULL DEFAULT 0,
  payload TEXT,
  PRIMARY KEY (symbol_key, target)
);

CREATE TABLE IF NOT EXISTS rate_limits (
  user_id INTEGER NOT NULL,
  window_start INTEGER NOT NULL,        -- unix minute
  count INTEGER NOT NULL,
  PRIMARY KEY (user_id, window_start)
);
