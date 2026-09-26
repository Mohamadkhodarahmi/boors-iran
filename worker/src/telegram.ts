/** Telegram webhook: commands, users, admin, rate limiting. Replies are plain text (no parse_mode)
 * so symbols and numbers never break Markdown/HTML parsing. */
import { getAnalysis } from "./api";
import { Env, json, nowIso, safeEqual, symbolKey } from "./util";

const DISCLAIMER = "این ربات خروجی خودکار تحلیل داده است و توصیه خرید یا فروش نیست.";

const HELP = [
  "🤖 تحلیلگر خودکار بورس تهران",
  "",
  "/market — وضعیت کلی بازار",
  "/top — بالاترین Model Score ها",
  "/scan — اسکن بازار (حجم، پول حقیقی، قدرت خریدار، شکست، بنیادی، ریسک)",
  "/stock نماد — تحلیل کامل (مثال: /stock فولاد)",
  "/tablo نماد — تحلیل تابلو و رفتار پول (کوتاه‌مدت)",
  "/technical نماد — وضعیت تکنیکال",
  "/fundamental نماد — وضعیت بنیادی از کدال",
  "",
  "Model Score احتمال نیست؛ عدد احتمال فقط برای مدل‌های calibrate‌شده نمایش داده می‌شود.",
  DISCLAIMER,
].join("\n");

interface TgUser { id: number; username?: string; first_name?: string }
interface TgMessage { message_id: number; chat: { id: number; type: string }; from?: TgUser; text?: string }
interface TgUpdate { update_id: number; message?: TgMessage }

export async function sendMessage(env: Env, chatId: number, text: string): Promise<void> {
  // Telegram limit is 4096 chars; split on line boundaries.
  const chunks: string[] = [];
  let cur = "";
  for (const line of text.split("\n")) {
    if ((cur + "\n" + line).length > 3900) {
      chunks.push(cur);
      cur = line;
    } else {
      cur = cur ? `${cur}\n${line}` : line;
    }
  }
  if (cur) chunks.push(cur);
  for (const chunk of chunks) {
    const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text: chunk, disable_web_page_preview: true }),
    });
    if (!r.ok) console.log("telegram sendMessage failed", r.status, await r.text());
  }
}

function adminIds(env: Env): Set<number> {
  return new Set((env.ADMIN_IDS ?? "").split(",").map((s) => Number(s.trim())).filter(Boolean));
}

async function upsertUser(env: Env, u: TgUser): Promise<{ is_admin: number; is_approved: number; is_blocked: number }> {
  const isAdmin = adminIds(env).has(u.id) ? 1 : 0;
  await env.DB.prepare(
    `INSERT INTO users (id, username, first_name, is_admin, is_approved, last_seen, request_count)
     VALUES (?1, ?2, ?3, ?4, ?4, ?5, 1)
     ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name,
       is_admin=?4, last_seen=excluded.last_seen, request_count=request_count + 1`,
  ).bind(u.id, u.username ?? null, u.first_name ?? null, isAdmin, nowIso()).run();
  return (await env.DB.prepare("SELECT is_admin, is_approved, is_blocked FROM users WHERE id = ?1")
    .bind(u.id).first<{ is_admin: number; is_approved: number; is_blocked: number }>())!;
}

/** Fixed-window counter per user per minute, stored in D1. */
async function allowRequest(env: Env, userId: number): Promise<boolean> {
  const limit = Number(env.RATE_LIMIT_PER_MIN ?? "12");
  const minute = Math.floor(Date.now() / 60000);
  const row = await env.DB.prepare(
    `INSERT INTO rate_limits (user_id, window_start, count) VALUES (?1, ?2, 1)
     ON CONFLICT(user_id, window_start) DO UPDATE SET count = count + 1
     RETURNING count`,
  ).bind(userId, minute).first<{ count: number }>();
  if (Math.random() < 0.02) {
    await env.DB.prepare("DELETE FROM rate_limits WHERE window_start < ?1").bind(minute - 60).run();
  }
  return (row?.count ?? 0) <= limit;
}

function freshness(asof: unknown): string {
  if (typeof asof !== "string") return "";
  const ageH = (Date.now() - Date.parse(asof + (asof.endsWith("Z") ? "" : "+03:30"))) / 3_600_000;
  return ageH > 30 ? `\n⚠️ این تحلیل مربوط به ${asof.slice(0, 16)} است و ممکن است به‌روز نباشد.` : "";
}

async function stockReply(env: Env, arg: string, kind: "stock" | "tablo" | "technical" | "fundamental"): Promise<string> {
  if (!arg) return `نماد را وارد کنید. مثال: /${kind} فولاد`;
  const mode = kind === "tablo" ? "short" : kind === "fundamental" ? "fundamental" : "swing";
  const row = await getAnalysis(env, arg, mode);
  if (!row) {
    const like = await env.DB.prepare("SELECT symbol FROM stocks WHERE symbol_key LIKE ?1 LIMIT 5")
      .bind(`%${symbolKey(arg)}%`).all<{ symbol: string }>();
    const hint = like.results.length ? `\nنمادهای مشابه: ${like.results.map((r) => r.symbol).join("، ")}` : "";
    return `برای «${arg}» تحلیلی موجود نیست (داده در دسترس نیست).${hint}`;
  }
  if (kind === "technical") {
    const s = (row.sections ?? {}) as Record<string, unknown>;
    const lines = (s.technical as string[] | undefined) ?? ["داده در دسترس نیست"];
    return [`📈 ${row.symbol} — تکنیکال`, ...lines, "", `⏱ Data: ${row.data_time ?? "-"}`, DISCLAIMER].join("\n")
      + freshness(row.asof);
  }
  return String(row.text) + freshness(row.asof);
}

async function adminCommand(env: Env, cmd: string, arg: string): Promise<string> {
  const id = Number(arg);
  switch (cmd) {
    case "/stats": {
      const u = await env.DB.prepare("SELECT COUNT(*) AS n, SUM(request_count) AS r FROM users").first<{ n: number; r: number }>();
      const a = await env.DB.prepare("SELECT COUNT(*) AS n, MAX(updated_at) AS t FROM stock_analysis").first<{ n: number; t: string }>();
      return `کاربران: ${u?.n ?? 0}\nدرخواست‌ها: ${u?.r ?? 0}\nتحلیل‌های ذخیره‌شده: ${a?.n ?? 0}\nآخرین ingest: ${a?.t ?? "-"}`;
    }
    case "/approve":
    case "/block":
    case "/unblock": {
      if (!id) return "شناسه عددی کاربر را وارد کنید.";
      const col = cmd === "/approve" ? "is_approved = 1" : cmd === "/block" ? "is_blocked = 1" : "is_blocked = 0";
      await env.DB.prepare(`UPDATE users SET ${col} WHERE id = ?1`).bind(id).run();
      return "انجام شد.";
    }
    default:
      return "دستور مدیریتی ناشناخته.";
  }
}

export async function handleTelegram(req: Request, env: Env): Promise<Response> {
  const secret = req.headers.get("x-telegram-bot-api-secret-token") ?? "";
  if (!env.TELEGRAM_WEBHOOK_SECRET || !safeEqual(secret, env.TELEGRAM_WEBHOOK_SECRET)) {
    return new Response("forbidden", { status: 403 });
  }
  const update = (await req.json()) as TgUpdate;
  const msg = update.message;
  if (!msg?.text || !msg.from) return json({ ok: true });
  const chatId = msg.chat.id;
  const user = await upsertUser(env, msg.from);
  if (user.is_blocked) return json({ ok: true });
  if (env.ACCESS_MODE === "allowlist" && !user.is_approved && !user.is_admin) {
    await sendMessage(env, chatId, `دسترسی شما هنوز تأیید نشده است. شناسه شما: ${msg.from.id}`);
    return json({ ok: true });
  }
  if (!user.is_admin && !(await allowRequest(env, msg.from.id))) {
    await sendMessage(env, chatId, "تعداد درخواست‌ها زیاد است؛ یک دقیقه دیگر تلاش کنید.");
    return json({ ok: true });
  }

  const [rawCmd, ...rest] = msg.text.trim().split(/\s+/);
  const cmd = rawCmd.split("@")[0].toLowerCase();
  const arg = rest.join(" ").slice(0, 40);
  let reply: string;
  try {
    switch (cmd) {
      case "/start":
      case "/help":
        reply = HELP;
        break;
      case "/market": {
        const row = await env.DB.prepare("SELECT text, asof FROM market_snapshots WHERE id='latest'").first<{ text: string; asof: string }>();
        reply = row ? row.text + freshness(row.asof) : "داده بازار هنوز دریافت نشده است.";
        break;
      }
      case "/scan":
      case "/top": {
        const row = await env.DB.prepare("SELECT text, top_text, asof FROM signals WHERE id='latest_scan'")
          .first<{ text: string; top_text: string | null; asof: string }>();
        reply = row ? (cmd === "/top" ? (row.top_text ?? row.text) : row.text) + freshness(row.asof)
          : "هنوز اسکنی انجام نشده است.";
        break;
      }
      case "/stock":
      case "/tablo":
      case "/technical":
      case "/fundamental":
        reply = await stockReply(env, arg, cmd.slice(1) as "stock" | "tablo" | "technical" | "fundamental");
        break;
      case "/stats":
      case "/approve":
      case "/block":
      case "/unblock":
        reply = user.is_admin ? await adminCommand(env, cmd, arg) : "این دستور فقط برای مدیر است.";
        break;
      default:
        // bare symbol -> full analysis
        reply = cmd.startsWith("/") ? HELP : await stockReply(env, msg.text.trim(), "stock");
    }
  } catch (e) {
    console.log("command failed", cmd, String(e));
    reply = "خطای داخلی؛ لطفاً بعداً تلاش کنید.";
  }
  try {
    await sendMessage(env, chatId, reply);
  } catch (e) {
    // Always answer 200: a non-2xx makes Telegram redeliver the same update repeatedly.
    console.log("telegram send failed", String(e));
  }
  return json({ ok: true });
}
