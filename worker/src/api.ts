/** Read-only JSON API (future dashboard). Protected by READ_TOKEN when that secret is set. */
import { Env, error, json, symbolKey } from "./util";

type Row = Record<string, unknown>;

function parse(row: Row | null, fields: string[]): Row | null {
  if (!row) return null;
  const out: Row = { ...row };
  for (const f of fields) {
    if (typeof out[f] === "string") {
      try {
        out[f] = JSON.parse(out[f] as string);
      } catch {
        /* keep raw */
      }
    }
  }
  return out;
}

export async function getAnalysis(env: Env, symbol: string, mode: string): Promise<Row | null> {
  const row = await env.DB.prepare("SELECT * FROM stock_analysis WHERE symbol_key = ?1 AND mode = ?2")
    .bind(symbolKey(symbol), mode).first<Row>();
  return parse(row, ["scores", "sections"]);
}

export async function handleApi(req: Request, env: Env, path: string): Promise<Response> {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const [head, arg] = parts;
  switch (head) {
    case "market": {
      const row = await env.DB.prepare("SELECT asof, data, text FROM market_snapshots WHERE id='latest'").first<Row>();
      return row ? json(parse(row, ["data"])) : error(404, "no market data yet");
    }
    case "stocks": {
      const { results } = await env.DB.prepare(
        "SELECT symbol, name, sector, market, updated_at FROM stocks ORDER BY symbol").all<Row>();
      return json({ items: results });
    }
    case "signals": {
      const row = await env.DB.prepare("SELECT asof, data FROM signals WHERE id='latest_scan'").first<Row>();
      return row ? json(parse(row, ["data"])) : error(404, "no scan yet");
    }
    case "stock":
    case "analysis": {
      if (!arg) return error(400, "symbol required");
      const url = new URL(req.url);
      const mode = url.searchParams.get("mode") ?? "swing";
      const row = await getAnalysis(env, arg, mode);
      return row ? json(row) : error(404, "symbol not analysed");
    }
    case "tablo":
    case "technical":
    case "fundamental": {
      if (!arg) return error(400, "symbol required");
      const mode = head === "tablo" ? "short" : head === "fundamental" ? "fundamental" : "swing";
      const row = await getAnalysis(env, arg, mode);
      if (!row) return error(404, "symbol not analysed");
      const sections = (row.sections ?? {}) as Row;
      return json({ symbol: row.symbol, asof: row.asof, data_time: row.data_time, [head]: sections[head] ?? null });
    }
    default:
      return error(404, "not found");
  }
}
