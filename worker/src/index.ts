import { handleApi } from "./api";
import { ingestAnalysis, ingestCodal, ingestMarket, ingestSignals } from "./ingest";
import { handleTelegram } from "./telegram";
import { bearerOk, Env, error, HttpError, json } from "./util";

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    try {
      if (path === "/" || path === "/health") return json({ ok: true, service: "bors-analyser" });

      if (path === "/telegram/webhook" && req.method === "POST") return await handleTelegram(req, env);

      if (path.startsWith("/api/ingest/")) {
        if (req.method !== "POST") return error(405, "method not allowed");
        if (!bearerOk(req, env.INGEST_TOKEN)) return error(401, "unauthorized");
        switch (path) {
          case "/api/ingest/analysis": return await ingestAnalysis(req, env);
          case "/api/ingest/market": return await ingestMarket(req, env);
          case "/api/ingest/signals": return await ingestSignals(req, env);
          case "/api/ingest/codal": return await ingestCodal(req, env);
          default: return error(404, "unknown ingest endpoint");
        }
      }

      if (req.method === "GET") {
        if (env.READ_TOKEN && !bearerOk(req, env.READ_TOKEN)) return error(401, "unauthorized");
        const apiPath = path.startsWith("/api/") ? path.slice(4) : path;
        return await handleApi(req, env, apiPath);
      }
      return error(404, "not found");
    } catch (e) {
      if (e instanceof HttpError) return error(e.status, e.message);
      console.log("unhandled", String(e));
      return error(500, "internal error");
    }
  },
} satisfies ExportedHandler<Env>;
