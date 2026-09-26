import {
  arr, batchRun, Env, HttpError, json, nowIso, num, oneOf, readJson, str, symbolKey,
} from "./util";

const MODES = ["short", "swing", "fundamental"] as const;

/** POST /api/ingest/analysis — latest analysis per symbol and mode from the local engine. */
export async function ingestAnalysis(req: Request, env: Env): Promise<Response> {
  const body = (await readJson(req)) as Record<string, unknown>;
  const items = arr(body.items, "items", 5000);
  const now = nowIso();
  const stmts: D1PreparedStatement[] = [];
  for (const [i, raw] of items.entries()) {
    const it = raw as Record<string, unknown>;
    const symbol = str(it.symbol, `items[${i}].symbol`, 40)!;
    const key = symbolKey(symbol);
    const mode = oneOf(it.mode, `items[${i}].mode`, MODES);
    const text = str(it.text, `items[${i}].text`, 8000)!;
    stmts.push(
      env.DB.prepare(
        `INSERT INTO stocks (symbol_key, symbol, name, sector, market, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(symbol_key) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
           sector=excluded.sector, market=excluded.market, updated_at=excluded.updated_at`,
      ).bind(key, symbol, str(it.name, "name", 200, true), str(it.sector, "sector", 200, true),
        str(it.market, "market", 60, true), now),
      env.DB.prepare(
        `INSERT INTO stock_analysis (symbol_key, mode, symbol, asof, data_time, label, overall_score, risk_score,
           scores, sections, text, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
         ON CONFLICT(symbol_key, mode) DO UPDATE SET symbol=excluded.symbol, asof=excluded.asof,
           data_time=excluded.data_time, label=excluded.label, overall_score=excluded.overall_score,
           risk_score=excluded.risk_score, scores=excluded.scores, sections=excluded.sections,
           text=excluded.text, updated_at=excluded.updated_at`,
      ).bind(key, mode, symbol, str(it.asof, "asof", 40)!, str(it.data_time, "data_time", 60, true),
        str(it.label, "label", 40, true), num(it.overall_score, "overall_score"),
        num(it.risk_score, "risk_score"), JSON.stringify(it.scores ?? {}), JSON.stringify(it.sections ?? {}),
        text, now),
    );
    const preds = (it.model ?? {}) as Record<string, Record<string, unknown>>;
    for (const [target, p] of Object.entries(preds)) {
      const calibrated = p.calibrated === true;
      stmts.push(
        env.DB.prepare(
          `INSERT INTO model_predictions (symbol_key, target, asof, model_score, probability, calibrated, payload)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
           ON CONFLICT(symbol_key, target) DO UPDATE SET asof=excluded.asof, model_score=excluded.model_score,
             probability=excluded.probability, calibrated=excluded.calibrated, payload=excluded.payload`,
        ).bind(key, str(target, "target", 40)!, str(it.asof, "asof", 40)!, num(p.model_score, "model_score"),
          // a probability is stored only for calibrated models
          calibrated ? num(p.probability, "probability") : null, calibrated ? 1 : 0, JSON.stringify(p)),
      );
    }
  }
  await batchRun(env.DB, stmts);
  return json({ ok: true, received: items.length });
}

/** POST /api/ingest/market — latest market overview. */
export async function ingestMarket(req: Request, env: Env): Promise<Response> {
  const body = (await readJson(req)) as Record<string, unknown>;
  const text = str(body.text, "text", 6000)!;
  await env.DB.prepare(
    `INSERT INTO market_snapshots (id, asof, data, text, updated_at) VALUES ('latest', ?1, ?2, ?3, ?4)
     ON CONFLICT(id) DO UPDATE SET asof=excluded.asof, data=excluded.data, text=excluded.text,
       updated_at=excluded.updated_at`,
  ).bind(str(body.asof, "asof", 40)!, JSON.stringify(body.data ?? {}), text, nowIso()).run();
  if (typeof body.date === "string" && typeof body.summary === "string") {
    await env.DB.prepare(
      `INSERT INTO daily_summaries (date, text, data) VALUES (?1, ?2, ?3)
       ON CONFLICT(date) DO UPDATE SET text=excluded.text, data=excluded.data`,
    ).bind(body.date, body.summary, JSON.stringify(body.data ?? {})).run();
  }
  return json({ ok: true });
}

/** POST /api/ingest/signals — latest market scan. */
export async function ingestSignals(req: Request, env: Env): Promise<Response> {
  const body = (await readJson(req)) as Record<string, unknown>;
  await env.DB.prepare(
    `INSERT INTO signals (id, asof, data, text, top_text, updated_at) VALUES ('latest_scan', ?1, ?2, ?3, ?4, ?5)
     ON CONFLICT(id) DO UPDATE SET asof=excluded.asof, data=excluded.data, text=excluded.text,
       top_text=excluded.top_text, updated_at=excluded.updated_at`,
  ).bind(str(body.asof, "asof", 40)!, JSON.stringify(body.data ?? {}), str(body.text, "text", 8000)!,
    str(body.top_text, "top_text", 8000, true), nowIso()).run();
  return json({ ok: true });
}

/** POST /api/ingest/codal — recent disclosures. */
export async function ingestCodal(req: Request, env: Env): Promise<Response> {
  const body = (await readJson(req)) as Record<string, unknown>;
  const items = arr(body.items, "items", 5000);
  const stmts = items.map((raw, i) => {
    const it = raw as Record<string, unknown>;
    const tracing = num(it.tracing_no, `items[${i}].tracing_no`);
    if (tracing === null) throw new HttpError(422, `items[${i}].tracing_no is required`);
    const symbol = str(it.symbol, "symbol", 40)!;
    return env.DB.prepare(
      `INSERT INTO codal_events (tracing_no, symbol_key, symbol, title, report_type, published_at, url)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
       ON CONFLICT(tracing_no) DO UPDATE SET title=excluded.title, report_type=excluded.report_type`,
    ).bind(tracing, symbolKey(symbol), symbol, str(it.title, "title", 1000)!, str(it.report_type, "report_type", 60, true),
      str(it.published_at, "published_at", 40, true), str(it.url, "url", 1000, true));
  });
  await batchRun(env.DB, stmts);
  return json({ ok: true, received: items.length });
}
