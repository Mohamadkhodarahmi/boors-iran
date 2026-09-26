"""CODAL collector.

- Letters (disclosures) come from `search.codal.ir/api/search/v2/q` (reverse-engineered, see
  amirziveh/codal-skill). Always queried with `Mains=true, Childs=false` because a plain symbol search
  also returns subsidiaries' letters.
- Structured statements are read from the `var datasource = {...}` JSON embedded in report pages and
  validated with `codalpy.models.DataSource` (yghaderi/codalpy). Sheet ids: 0 balance sheet,
  1 income statement, 9 cash flow. Values are in **million Rial**.
- Pages without a datasource (e.g. many consolidated annual reports) are recorded as
  `parse_status='skip'` — never guessed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx
from codalpy.models import DataSource
from sqlalchemy import select

from bors_analyser.config import PROJECT_ROOT, get_settings
from bors_analyser.db.models import CodalLetter, FinancialStatement, Instrument
from bors_analyser.db.session import FetchAudit, session_scope, upsert
from bors_analyser.net import iran_client
from bors_analyser.timeutil import jalali_to_date, jalali_to_datetime, normalize_fa, now_tehran

log = logging.getLogger(__name__)

SEARCH_URL = "https://search.codal.ir/api/search/v2/q"
BASE_URL = "https://www.codal.ir"
SHEETS = {"balance_sheet": "0", "income": "1", "cash_flow": "9"}
UNIT = "million_rial"

_DATASOURCE_RE = re.compile(r"var datasource = (.*?);\s*\n", re.S)
_DATASOURCE_RE_LOOSE = re.compile(r"var datasource = (.*?);")


def _naive_now() -> datetime:
    return now_tehran().replace(tzinfo=None, microsecond=0)


def key(text: str | None) -> str:
    """Aggressive normalization for matching Persian labels (spaces, ZWNJ, punctuation removed)."""
    t = normalize_fa(text).replace("آ", "ا").replace("ة", "ه").replace("ؤ", "و").replace("إ", "ا").replace("أ", "ا")
    return re.sub(r"[\s‌‍­‏():\-–—_،,.«»\"'/]+", "", t)


def _title_norm(title: str) -> str:
    return re.sub(r"\s+", " ", normalize_fa(title).replace("‌", " ")).strip()


# --------------------------------------------------------------------------- classification

# (report_type, [title substrings in key() form]) — first match wins.
REPORT_TYPES: list[tuple[str, list[str]]] = [
    # Checked before financial statements: these titles mention "صورت‌های مالی" but are not statements.
    ("statement_explanation", ["توضیحاتدرخصوصاطلاعاتوصورتهایمالی"]),
    ("major_disclosure", ["افشایاطلاعاتبااهمیت"]),
    ("financial_statements", ["صورتهایمالی", "اطلاعاتوصورتهایمالی"]),
    ("monthly_activity", ["فعالیتماهانه"]),
    ("portfolio", ["صورتوضعیتپورتفوی"]),
    ("capital_increase", ["افزایشسرمایه", "حقتقدم", "پذیرهنویسی"]),
    ("dividend", ["زمانبندیپرداختسود"]),
    ("assembly", ["مجمع"]),
    ("forecast", ["پیشبینیدرآمد", "پیشبینی"]),
    ("board_report", ["گزارشفعالیتهیئتمدیره", "گزارشفعالیتهیاتمدیره", "گزارشتفسیری"]),
    ("clarification", ["شفافسازی"]),
    ("trading_status", ["توقفنماد", "تعلیقنماد", "بازگشایینماد", "تعلیقورقه"]),
    ("major_disclosure", ["افشایاطلاعاتبااهمیت", "اطلاعاتبااهمیت"]),
    ("board_change", ["ترکیباعضایهیئتمدیره", "ترکیبهیئتمدیره", "معرفییاتغییر"]),
    ("auditor", ["حسابرس", "بازرسقانونی"]),
]

EVENT_TAGS: dict[str, list[str]] = {
    "capital_increase": ["افزایشسرمایه"],
    "major_disclosure": ["افشایاطلاعاتبااهمیت", "اطلاعاتبااهمیت"],
    "major_contract": ["قرارداد"],
    "assembly": ["مجمع"],
    "dividend": ["زمانبندیپرداختسود", "تقسیمسود"],
    "clarification": ["شفافسازی"],
    "price_fluctuation": ["نوسانقیمت"],
    "halt": ["توقفنماد"],
    "suspension": ["تعلیق"],
    "reopening": ["بازگشایی"],
    "forecast": ["پیشبینی"],
    "consolidated": ["تلفیقی"],
    "audited": ["حسابرسیشده"],
    "not_audited": ["حسابرسینشده"],
    "correction": ["اصلاحیه"],
    "conference": ["کنفرانساطلاعرسانی"],
    "auditor_issue": ["اظهارنظرمشروط", "عدماظهارنظر", "اظهارنظرمردود", "مشروط"],
    "board_change": ["ترکیباعضایهیئتمدیره", "تغییرمدیرعامل"],
    "related_party": ["اشخاصوابسته"],
}

_PERIOD_RE = re.compile(r"(\d+)\s*ماهه\s*منتهی\s*به\s*(\d{4}/\d{2}/\d{2})")
_FY_RE = re.compile(r"سال\s*مالی\s*منتهی\s*به\s*(\d{4}/\d{2}/\d{2})")


def classify_letter(title: str) -> dict[str, Any]:
    t = _title_norm(title)
    k = key(title)
    report_type = "other"
    for rtype, needles in REPORT_TYPES:
        if any(n in k for n in needles):
            report_type = rtype
            break
    tags = sorted({tag for tag, needles in EVENT_TAGS.items() if any(n in k for n in needles)})
    if "not_audited" in tags and "audited" in tags:
        tags.remove("audited")
    period_months, period_end = None, None
    if m := _PERIOD_RE.search(t):
        period_months, period_end = int(m.group(1)), jalali_to_date(m.group(2))
    elif m := _FY_RE.search(t):
        period_months, period_end = 12, jalali_to_date(m.group(1))
    audited = True if "audited" in tags else (False if "not_audited" in tags else None)
    return {
        "report_type": report_type,
        "event_tags": tags,
        "period_months": period_months,
        "period_end": period_end,
        "audited": audited,
        "is_correction": "correction" in tags,
    }


def letter_row(L: dict[str, Any], symbol_to_code: dict[str, str], now: datetime) -> dict[str, Any]:
    symbol = normalize_fa(L.get("Symbol"))
    cls = classify_letter(L.get("Title", ""))

    def url(u: str | None) -> str | None:
        if not u:
            return None
        return u if u.startswith("http") else f"{BASE_URL}/{u.lstrip('/')}"

    return {
        "tracing_no": int(L["TracingNo"]),
        "symbol": symbol,
        "company_name": normalize_fa(L.get("CompanyName")),
        "ins_code": symbol_to_code.get(symbol),
        "letter_code": normalize_fa(L.get("LetterCode")),
        "title": _title_norm(L.get("Title", "")),
        "sent_at": jalali_to_datetime(L["SentDateTime"]).replace(tzinfo=None) if L.get("SentDateTime") else None,
        "published_at": jalali_to_datetime(L["PublishDateTime"]).replace(tzinfo=None)
        if L.get("PublishDateTime") else None,
        "url": url(L.get("Url")),
        "attachment_url": url(L.get("AttachmentUrl")),
        "pdf_url": url(L.get("PdfUrl")),
        "excel_url": url(L.get("ExcelUrl")),
        "raw_data": L,
        "source": "codal",
        "fetched_at": now,
        **cls,
    }


# --------------------------------------------------------------------------- statement parsing


def extract_datasource(html: str) -> DataSource | None:
    m = _DATASOURCE_RE.search(html) or _DATASOURCE_RE_LOOSE.search(html)
    if not m:
        return None
    return DataSource.model_validate_json(m.group(1))


def parse_number(v: str | None) -> float | None:
    if v is None:
        return None
    s = normalize_fa(v).replace(",", "").replace("٬", "").strip()
    if s in ("", "-", "--"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


# canonical item -> (exact keys, prefix keys)
ITEM_MAP: dict[str, dict[str, tuple[list[str], list[str]]]] = {
    "income": {
        "revenue": (["درامدهایعملیاتی", "جمعدرامدهایعملیاتی", "فروشخالص", "درامدعملیاتی", "فروشخالصودرامدارائهخدمات"], []),
        "cost_of_revenue": ([], ["بهایتمامشده"]),
        "gross_profit": (["سودزیانناخالص", "سودناخالص"], []),
        "sga": ([], ["هزینههایفروش"]),
        "operating_profit": (["سودزیانعملیاتی", "سودعملیاتی"], []),
        "financial_costs": (["هزینههایمالی"], []),
        "pretax_profit": ([], ["سودزیانعملیاتدرحالتداومقبلازمالیات", "سودزیانقبلازمالیات"]),
        "net_profit": (["سودزیانخالص", "سودخالص"], []),
        "eps": (["سودزیانپایههرسهم", "سودپایههرسهم"], []),
        "eps_diluted_or_net": ([], ["سودزیانخالصهرسهم"]),
        "capital": (["سرمایه"], []),
    },
    "balance_sheet": {
        "total_assets": (["جمعداراییها"], []),
        "current_assets": (["جمعداراییهایجاری"], []),
        "noncurrent_assets": (["جمعداراییهایغیرجاری"], []),
        "cash": (["موجودینقد"], []),
        "inventory": (["موجودیموادوکالا"], []),
        "receivables": ([], ["دریافتنیهایتجاری"]),
        "total_liabilities": (["جمعبدهیها"], []),
        "current_liabilities": (["جمعبدهیهایجاری"], []),
        "short_term_debt": (["تسهیلاتمالی"], []),
        "long_term_debt": (["تسهیلاتمالیبلندمدت"], []),
        "equity": (["جمعحقوقمالکانه", "جمعحقوقصاحبانسهام"], []),
        "capital": (["سرمایه"], []),
        "retained_earnings": (["سودزیانانباشته"], []),
    },
    "cash_flow": {
        "operating_cash_flow": ([], ["جریانخالصورودخروجنقدحاصلازفعالیتهایعملیاتی", "جریانخالصورودخروجوجهنقدناشیازفعالیتهایعملیاتی"]),
        "cash_from_operations": (["نقدحاصلازعملیات"], []),
        "capex": ([], ["پرداختهاینقدیبرایخریدداراییهایثابتمشهود"]),
        "investing_cash_flow": ([], ["جریانخالصورودخروجنقدحاصلازفعالیتهایسرمایهگذاری"]),
        "financing_cash_flow": ([], ["جریانخالصورودخروجنقدحاصلازفعالیتهایتامینمالی"]),
        "dividends_paid": (["پرداختهاینقدیبابتسودسهام"], []),
    },
}


def _match_item(label_key: str, mapping: dict[str, tuple[list[str], list[str]]]) -> str | None:
    for item, (exact, _prefixes) in mapping.items():
        if label_key in exact:
            return item
    for item, (_, prefixes) in mapping.items():
        if any(label_key.startswith(p) for p in prefixes):
            return item
    return None


def _table_grid(ds: DataSource) -> list[dict[int, Any]]:
    """Rows of all tables as {column_sequence: cell}, ordered by row_sequence."""
    rows: dict[tuple[int, int], dict[int, Any]] = {}
    for sh in ds.sheets:
        for ti, tb in enumerate(sh.tables):
            for c in tb.cells:
                rows.setdefault((ti, c.row_sequence), {})[c.column_sequence] = c
    return [rows[k] for k in sorted(rows)]


def normalize_statement(ds: DataSource, sheet: str) -> dict[str, Any]:
    """Map a statement to canonical items for every value column.

    Returns {columns: [{header, period_end, audited, restated, items}], current, prior_same_period}.
    """
    mapping = ITEM_MAP[sheet]
    grid = _table_grid(ds)
    columns: dict[int, dict[str, Any]] = {}
    for row in grid:
        for col, cell in row.items():
            if col == 1 or col in columns:
                continue
            header = normalize_fa(cell.value)
            if cell.period_end_to_date and header and "درصد" not in header and not header.replace(" ", "").isdigit():
                if "حسابرسی" in header and not any(x in header for x in ("دوره", "تاریخ", "سال")):
                    continue  # the audited/unaudited sub-header row
                columns[col] = {
                    "col": col, "header": header, "period_end": cell.period_end_to_date,
                    "restated": "تجدید" in header, "audited": None, "items": {},
                }
    for row in grid:
        for col, cell in row.items():
            if col in columns and "حسابرسی" in (v := normalize_fa(cell.value)):
                columns[col]["audited"] = "نشده" not in v
        label = row.get(1)
        if label is None:
            continue
        item = _match_item(key(label.value), mapping)
        if item is None:
            continue
        for col, meta in columns.items():
            if item in meta["items"] and meta["items"][item] is not None:
                continue  # keep first numeric occurrence (e.g. EPS header row has no values)
            cell = row.get(col)
            meta["items"][item] = parse_number(cell.value) if cell else None

    cols = sorted(columns.values(), key=lambda c: c["col"])
    pe = ds.period_end_to_date
    current = next((c for c in cols if c["period_end"] == pe and not c["restated"]), None) or (cols[0] if cols else None)
    prior = None
    if current:
        try:
            cy, cm, _ = (int(x) for x in normalize_fa(current["period_end"]).split("/"))
        except ValueError:
            cy = None
        if cy:
            if sheet == "balance_sheet":
                fy = normalize_fa(ds.year_end_to_date)
                prev_fy = f"{int(fy[:4]) - 1}{fy[4:]}" if fy else None
                cands = [c for c in cols if c is not current and c["period_end"] in (prev_fy,)]
                cands = cands or [c for c in cols if c is not current and c["period_end"] < current["period_end"]]
            else:
                target = f"{cy - 1}/{cm:02d}"
                cands = [c for c in cols if c is not current and c["period_end"].startswith(target)]
            prior = next((c for c in cands if c["restated"]), cands[0] if cands else None)
    return {
        "unit": UNIT,
        "period_end": pe,
        "fiscal_year_end": ds.year_end_to_date,
        "period_months": ds.period,
        "consolidated": ds.isConsolidated,
        "columns": [{k: v for k, v in c.items() if k != "col"} for c in cols],
        "current": current["items"] if current else {},
        "prior_same_period": prior["items"] if prior else None,
        "prior_period_end": prior["period_end"] if prior else None,
    }


def parse_monthly_sales(ds: DataSource) -> dict[str, Any]:
    """Monthly activity report -> month / YTD / prior-YTD sales (million Rial).

    Works for production/sales templates (table with «فروش») and bank templates («درآمدهای عملیاتی»).
    Missing parts are returned as None.
    """
    out: dict[str, Any] = {"unit": UNIT, "month_sales": None, "ytd_sales": None, "prior_ytd_sales": None,
                           "table": None, "template": None}
    for sh in ds.sheets:
        for tb in sh.tables:
            title = normalize_fa(tb.title_fa or "")
            if "فروش" not in title and "درآمد" not in title:
                continue
            rows: dict[int, dict[int, str]] = {}
            for c in tb.cells:
                rows.setdefault(c.row_sequence, {})[c.column_sequence] = normalize_fa(c.value)
            seqs = sorted(rows)
            if len(seqs) < 3:
                continue
            h1, h2 = rows[seqs[0]], rows[seqs[1]]
            groups = sorted((col, txt) for col, txt in h1.items() if col > 1 and txt)
            if not groups:
                continue

            def amount_col(start: int, end: int, h2: dict[int, str] = h2) -> int:
                for col in range(start, end):
                    if "مبلغ" in h2.get(col, ""):
                        return col
                return start

            bounds = [(g[0], groups[i + 1][0] if i + 1 < len(groups) else 10_000, g[1]) for i, g in enumerate(groups)]
            month_c = ytd_c = prior_c = None
            ytd_dates: list[tuple[str, int]] = []
            for start, end, txt in bounds:
                k = key(txt)
                if "یکماهه" in k or "ماههمنتهی" in k:
                    month_c = amount_col(start, end)
                elif "ابتدایسالمالی" in k and "اصلاح" not in k and "اصلاحات" not in k:
                    if dm := re.search(r"(\d{4}/\d{2}/\d{2})", txt):
                        ytd_dates.append((dm.group(1), amount_col(start, end)))
            if ytd_dates:
                ytd_dates.sort()
                latest = ytd_dates[-1][0]
                ytd_c = ytd_dates[-1][1]
                prev_year = f"{int(latest[:4]) - 1}{latest[4:]}"
                prior_c = next((c for d, c in ytd_dates if d == prev_year), None)
            total = None
            for rs in reversed(seqs):
                lab = key(rows[rs].get(1, ""))
                if lab in ("جمع", "جمعکل") and any(parse_number(v) is not None for c, v in rows[rs].items() if c > 1):
                    total = rows[rs]
                    break
            if total is None:
                continue
            out.update({
                "table": title,
                "template": "production" if "فروش" in title else "bank",
                "month_sales": parse_number(total.get(month_c)) if month_c else None,
                "ytd_sales": parse_number(total.get(ytd_c)) if ytd_c else None,
                "prior_ytd_sales": parse_number(total.get(prior_c)) if prior_c else None,
            })
            return out
    return out


# --------------------------------------------------------------------------- HTTP


class Pacer:
    """Minimum interval between requests (shared by all coroutines of one client)."""

    def __init__(self, interval_s: float):
        self.interval_s = interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._last + self.interval_s - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = loop.time()


class CodalClient:
    """CODAL's WAF throttles *search.codal.ir* per IP (HTTP 429, sometimes for > 45 minutes), while
    report pages on www.codal.ir tolerate more. Each host therefore gets its own pacer."""

    def __init__(self, client: httpx.AsyncClient, search_interval_s: float | None = None,
                 report_interval_s: float | None = None):
        s = get_settings()
        self.client = client
        self.search_pacer = Pacer(s.codal_search_interval_s if search_interval_s is None else search_interval_s)
        self.report_pacer = Pacer(s.codal_report_interval_s if report_interval_s is None else report_interval_s)

    async def _page(self, params: dict[str, Any], page: int) -> dict[str, Any]:
        """One search page; on 429 wait progressively longer (30 s ... 5 min) before giving up."""
        base = {"search": "true", "PublisherType": 1, "Mains": "true", "Childs": "false", "Publisher": "false"}
        waits = [30, 60, 120, 240, 300, 300]
        for i, wait in enumerate([*waits, 0]):
            try:
                await self.search_pacer.wait()
                r = await self.client.get(SEARCH_URL, params=base | params | {"PageNumber": page})
                if r.status_code == 429:
                    if i == len(waits):
                        break
                    log.info("codal search rate-limited (page %d), waiting %ds", page, wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.HTTPStatusError):
                if i >= 3:
                    raise
                await asyncio.sleep(5 * (i + 1))
        raise RuntimeError(f"codal search page {page}: still rate-limited")

    async def search_pages(
        self,
        params: dict[str, Any],
        on_page: Callable[[int, list[dict[str, Any]]], int],
        max_pages: int = 1,
        start_page: int = 1,
        stop_when_known: bool = False,
    ) -> int:
        """Fetch pages sequentially (newest letters first) and hand each page to `on_page` at once, so an
        interruption never loses what was already fetched. `on_page` returns the number of new letters;
        with `stop_when_known` paging stops at the first page without new letters.
        Returns the last page fetched."""
        page, last = start_page, start_page - 1
        while page <= max_pages:
            data = await self._page(params, page)
            new = on_page(page, data.get("Letters") or [])
            last = page
            total_pages = int(data.get("Page") or 1)
            if page >= total_pages or (stop_when_known and new == 0):
                break
            page += 1
        return last

    async def search(self, max_pages: int = 1, **params: Any) -> list[dict[str, Any]]:
        letters: list[dict[str, Any]] = []

        def collect(_: int, page_letters: list[dict[str, Any]]) -> int:
            letters.extend(page_letters)
            return len(page_letters)

        await self.search_pages(params, collect, max_pages=max_pages)
        return letters

    async def report_datasource(self, url: str, sheet_id: str | None = None, attempts: int = 3) -> DataSource | None:
        """www.codal.ir report pages are slow at times; retry timeouts / 429 with backoff."""
        if sheet_id is not None:
            url = f"{url}&SheetId={sheet_id}"
        for i in range(attempts):
            try:
                await self.report_pacer.wait()
                r = await self.client.get(url, timeout=httpx.Timeout(60.0, connect=10.0))
                if r.status_code == 429:
                    await asyncio.sleep(min(120.0, 15.0 * 2**i))
                    continue
                r.raise_for_status()
                return extract_datasource(r.text)
            except (httpx.ReadTimeout, httpx.RemoteProtocolError):
                if i == attempts - 1:
                    raise
                await asyncio.sleep(2 * (i + 1))
        return None


def _symbol_map() -> dict[str, str]:
    with session_scope() as s:
        rows = s.execute(select(Instrument.symbol, Instrument.ins_code).where(Instrument.is_stock.is_(True))).all()
    return {sym: code for sym, code in rows}


def store_letters(letters: list[dict[str, Any]], smap: dict[str, str] | None = None) -> int:
    """Insert letters not seen before; never resets the parse status of known letters."""
    if not letters:
        return 0
    now = _naive_now()
    smap = smap if smap is not None else _symbol_map()
    rows = {r["tracing_no"]: r for r in (letter_row(L, smap, now) for L in letters)}
    with session_scope() as s:
        existing = set(s.execute(select(CodalLetter.tracing_no).where(
            CodalLetter.tracing_no.in_(list(rows)))).scalars())
        new = [r | {"parse_status": _initial_status(r)} for k, r in rows.items() if k not in existing]
        upsert(s, CodalLetter, new)
    return len(new)


_STATE_FILE = PROJECT_ROOT / "data" / "codal_bootstrap_state.json"


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


async def poll_letters(
    pages: int = 3,
    symbol: str | None = None,
    from_jdate: str | None = None,
    category: int | None = None,
    letter_type: int | None = None,
    stop_when_known: bool = False,
    resume_key: str | None = None,
) -> int:
    """Fetch letters page by page and store each page immediately.

    - `stop_when_known`: incremental polling — stop at the first page with nothing new, so a PC that
      was off for a while catches up automatically (up to `pages` pages).
    - `resume_key`: long bootstrap queries checkpoint their last completed page in
      data/codal_bootstrap_state.json and resume there after an interruption.
    """
    params: dict[str, Any] = {}
    if symbol:
        params["Symbol"] = symbol
    if from_jdate:
        params["FromDate"] = from_jdate
    if category is not None:
        params["Category"] = category
    if letter_type is not None:
        params["LetterType"] = letter_type
    target = f"{symbol or 'market'}:{category}:{letter_type}"
    state = _load_state() if resume_key else {}
    done = state.get(resume_key, {}) if resume_key else {}
    if done.get("finished"):
        log.info("codal %s: already completed earlier (delete %s to redo)", target, _STATE_FILE.name)
        return 0
    # pages shift as new letters arrive; restart one page early (duplicates are ignored)
    start = max(1, int(done.get("last_page", 0)) - 1)
    smap = _symbol_map()
    total_new = 0

    def on_page(page: int, letters: list[dict[str, Any]]) -> int:
        nonlocal total_new
        n = store_letters(letters, smap)
        total_new += n
        if resume_key:
            state[resume_key] = {"last_page": page, "finished": False}
            _save_state(state)
        if page % 20 == 0:
            log.info("codal %s: page %d, %d new so far", target, page, total_new)
        return n

    with FetchAudit("codal", "search", target) as audit:
        async with iran_client() as http:
            await CodalClient(http).search_pages(params, on_page, max_pages=pages, start_page=start,
                                                 stop_when_known=stop_when_known)
        audit.rows = total_new
    if resume_key:
        state[resume_key] = {"last_page": state.get(resume_key, {}).get("last_page", 0), "finished": True}
        _save_state(state)
    log.info("codal %s: %d new letters", target, total_new)
    return total_new


def _initial_status(row: dict[str, Any]) -> str:
    if row.get("ins_code") is None and "ins_code" in row:
        return "skip"  # not a stock in our universe (funds, unlisted publishers, ...)
    if row["report_type"] == "financial_statements" and "consolidated" not in (row["event_tags"] or []):
        return "pending"
    if row["report_type"] == "monthly_activity":
        return "pending"
    return "skip"


async def _parse_letter(codal: CodalClient, L: CodalLetter) -> list[dict[str, Any]]:
    now = _naive_now()
    base = {
        "tracing_no": L.tracing_no, "symbol": L.symbol, "published_at": L.published_at,
        "source": "codal", "fetched_at": now,
    }
    out: list[dict[str, Any]] = []
    if L.report_type == "monthly_activity":
        ds = await codal.report_datasource(L.url)
        if ds is None:
            raise LookupError("no datasource")
        norm = parse_monthly_sales(ds)
        out.append(base | {
            "sheet": "monthly_sales", "period_end": jalali_to_date(ds.period_end_to_date),
            "fiscal_year_end": jalali_to_date(ds.year_end_to_date), "period_months": ds.period,
            "audited": ds.is_audited, "consolidated": ds.isConsolidated,
            "raw_data": ds.model_dump(mode="json", by_alias=True), "normalized_data": norm,
            "data_quality": "ok" if norm["month_sales"] is not None else "partial",
        })
        return out
    for sheet, sid in SHEETS.items():
        ds = await codal.report_datasource(L.url, sid)
        if ds is None:
            if sheet == "balance_sheet":
                raise LookupError("no datasource")
            continue
        norm = normalize_statement(ds, sheet)
        quality = "ok" if norm["current"] and any(v is not None for v in norm["current"].values()) else "partial"
        out.append(base | {
            "sheet": sheet, "period_end": jalali_to_date(ds.period_end_to_date),
            "fiscal_year_end": jalali_to_date(ds.year_end_to_date), "period_months": ds.period,
            "audited": ds.is_audited, "consolidated": ds.isConsolidated,
            "raw_data": ds.model_dump(mode="json", by_alias=True), "normalized_data": norm,
            "data_quality": quality,
        })
    return out


async def bootstrap(monthly_from: str, statements_from: str, max_pages: int = 2000) -> dict[str, int]:
    """Initial market-wide load: monthly reports + interim statements + recent disclosures.

    Searching by letter type across all companies is far cheaper than per-symbol searches.
    """
    counts = {
        "monthly": await poll_letters(pages=max_pages, from_jdate=monthly_from, category=3, letter_type=58,
                                      resume_key=f"monthly:{monthly_from}"),
        "statements": await poll_letters(pages=max_pages, from_jdate=statements_from, category=1, letter_type=6,
                                         resume_key=f"statements:{statements_from}"),
        "recent_all": await poll_letters(pages=100, resume_key="recent_all"),
    }
    log.info("codal bootstrap letters: %s", counts)
    return counts


async def parse_pending(limit: int = 200, symbol: str | None = None, concurrency: int | None = None) -> dict[str, int]:
    """Download and normalize statements/monthly reports for pending letters (newest first)."""
    with session_scope() as s:
        q = select(CodalLetter).where(CodalLetter.parse_status.in_(["pending", "error"]))
        if symbol:
            q = q.where(CodalLetter.symbol == normalize_fa(symbol))
        letters = s.execute(q.order_by(CodalLetter.published_at.desc()).limit(limit)).scalars().all()
    sem = asyncio.Semaphore(concurrency or get_settings().codal_concurrency)
    stats = {"ok": 0, "skip": 0, "error": 0}

    async def one(codal: CodalClient, L: CodalLetter) -> None:
        async with sem:
            status, err, rows = "ok", None, []
            try:
                with FetchAudit("codal", f"parse_{L.report_type}", f"{L.symbol}:{L.tracing_no}") as a:
                    rows = await _parse_letter(codal, L)
                    a.rows = len(rows)
            except LookupError as e:
                status, err = "skip", str(e)
            except Exception as e:
                status, err = "error", repr(e)[:500]
            with session_scope() as s:
                upsert(s, FinancialStatement, rows)
                s.get(CodalLetter, L.tracing_no).parse_status = status
                s.get(CodalLetter, L.tracing_no).parse_error = err
            stats[status] += 1

    async with iran_client() as http:
        codal = CodalClient(http)
        await asyncio.gather(*(one(codal, L) for L in letters))
    log.info("codal parse: %s", stats)
    return stats


def reclassify_all() -> int:
    """Re-run title classification on stored letters (after rule changes)."""
    changed = 0
    with session_scope() as s:
        for L in s.execute(select(CodalLetter)).scalars():
            cls = classify_letter(L.title)
            if cls["report_type"] != L.report_type or cls["event_tags"] != L.event_tags:
                was_parsable = L.report_type in ("financial_statements", "monthly_activity")
                for k, v in cls.items():
                    setattr(L, k, v)
                new_status = _initial_status({"report_type": L.report_type, "event_tags": L.event_tags})
                if new_status == "skip" or not was_parsable:
                    L.parse_status = new_status
                changed += 1
    log.info("reclassified %d letters", changed)
    return changed


def latest_statement(symbol: str, sheet: str, before: date | None = None) -> FinancialStatement | None:
    with session_scope() as s:
        q = select(FinancialStatement).where(
            FinancialStatement.symbol == normalize_fa(symbol), FinancialStatement.sheet == sheet)
        if before:
            q = q.where(FinancialStatement.published_at < before)
        return s.execute(q.order_by(FinancialStatement.published_at.desc()).limit(1)).scalars().first()
