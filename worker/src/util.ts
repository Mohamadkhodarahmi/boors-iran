export interface Env {
  DB: D1Database;
  INGEST_TOKEN: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  READ_TOKEN?: string;
  ACCESS_MODE?: string;
  RATE_LIMIT_PER_MIN?: string;
  ADMIN_IDS?: string;
}

export function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export function error(status: number, message: string): Response {
  return json({ ok: false, error: message }, status);
}

/** Normalize Persian symbols for lookup: Arabic yeh/kaf, digits, spaces and ZWNJ. */
export function symbolKey(s: string): string {
  const digits = "۰۱۲۳۴۵۶۷۸۹";
  return s
    .trim()
    .replace(/ي|ى/g, "ی")
    .replace(/ك/g, "ک")
    .replace(/[۰-۹]/g, (d) => String(digits.indexOf(d)))
    .replace(/[\s‌‍‎‏]/g, "");
}

/** Constant-time string comparison (avoids leaking the token through timing). */
export function safeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  let diff = x.length ^ y.length;
  const n = Math.max(x.length, y.length);
  for (let i = 0; i < n; i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

export function bearerOk(req: Request, token: string | undefined): boolean {
  if (!token) return false;
  const h = req.headers.get("authorization") ?? "";
  const m = /^Bearer\s+(.+)$/i.exec(h);
  return !!m && safeEqual(m[1], token);
}

export async function readJson(req: Request, maxBytes = 8_000_000): Promise<unknown> {
  const len = Number(req.headers.get("content-length") ?? "0");
  if (len > maxBytes) throw new HttpError(413, "payload too large");
  const text = await req.text();
  if (text.length > maxBytes) throw new HttpError(413, "payload too large");
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "invalid JSON");
  }
}

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function nowIso(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------- tiny validators

export function str(v: unknown, field: string, max = 200, optional = false): string | null {
  if (v === undefined || v === null) {
    if (optional) return null;
    throw new HttpError(422, `${field} is required`);
  }
  if (typeof v !== "string" || v.length > max) throw new HttpError(422, `${field} must be a string <= ${max}`);
  return v;
}

export function num(v: unknown, field: string): number | null {
  if (v === undefined || v === null) return null;
  if (typeof v !== "number" || !Number.isFinite(v)) throw new HttpError(422, `${field} must be a finite number`);
  return v;
}

export function oneOf<T extends string>(v: unknown, field: string, allowed: readonly T[]): T {
  if (typeof v !== "string" || !allowed.includes(v as T)) {
    throw new HttpError(422, `${field} must be one of ${allowed.join(",")}`);
  }
  return v as T;
}

export function arr(v: unknown, field: string, max: number): unknown[] {
  if (!Array.isArray(v)) throw new HttpError(422, `${field} must be an array`);
  if (v.length > max) throw new HttpError(422, `${field} has more than ${max} items`);
  return v;
}

export async function batchRun(db: D1Database, stmts: D1PreparedStatement[], size = 50): Promise<void> {
  for (let i = 0; i < stmts.length; i += size) {
    await db.batch(stmts.slice(i, i + size));
  }
}
