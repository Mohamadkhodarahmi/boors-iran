"""Fundamental features from normalized CODAL statements and disclosures.

All statement amounts are million Rial. Every function takes `asof` and only uses documents
published before it (no look-ahead). Anything that cannot be computed is None — callers render
it as "data unavailable".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from bors_analyser.db.models import CodalLetter, FinancialStatement
from bors_analyser.db.session import session_scope
from bors_analyser.timeutil import normalize_fa

MILLION = 1_000_000


def growth(cur: float | None, prev: float | None) -> float | None:
    """Relative change; uses |prev| so a loss shrinking reads as positive growth."""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _prev_fy_column(norm: dict[str, Any]) -> dict[str, Any] | None:
    """Column of the previous full fiscal year inside an interim income statement."""
    fy = normalize_fa(norm.get("fiscal_year_end") or "")
    if not fy:
        return None
    prev_fy = f"{int(fy[:4]) - 1}{fy[4:]}"
    for c in norm.get("columns", []):
        if c["period_end"] == prev_fy:
            return c["items"]
    # fiscal year-end days differ in leap years (1403/12/30 vs 1404/12/29)
    for c in norm.get("columns", []):
        if c["period_end"][:7] == prev_fy[:7]:
            return c["items"]
    return None


def ttm(norm: dict[str, Any], item: str) -> float | None:
    """Trailing twelve months = previous FY + current YTD − prior-year YTD (or FY if 12m)."""
    cur = (norm.get("current") or {}).get(item)
    if norm.get("period_months") == 12:
        return cur
    prior = (norm.get("prior_same_period") or {}).get(item)
    fy = (_prev_fy_column(norm) or {}).get(item)
    if cur is None or prior is None or fy is None:
        return None
    return fy + cur - prior


def income_features(norm: dict[str, Any] | None) -> dict[str, Any]:
    if not norm:
        return {}
    cur, prev = norm.get("current") or {}, norm.get("prior_same_period") or {}
    rev = cur.get("revenue")
    out = {
        "period_end": norm.get("period_end"),
        "period_months": norm.get("period_months"),
        "revenue": rev,
        "net_profit": cur.get("net_profit"),
        "revenue_growth": growth(rev, prev.get("revenue")),
        "gross_profit_growth": growth(cur.get("gross_profit"), prev.get("gross_profit")),
        "operating_profit_growth": growth(cur.get("operating_profit"), prev.get("operating_profit")),
        "profit_growth": growth(cur.get("net_profit"), prev.get("net_profit")),
        "eps": cur.get("eps"),
        "eps_growth": growth(cur.get("eps"), prev.get("eps")),
        "gross_margin": ratio(cur.get("gross_profit"), rev),
        "operating_margin": ratio(cur.get("operating_profit"), rev),
        "net_margin": ratio(cur.get("net_profit"), rev),
        "gross_margin_prior": ratio(prev.get("gross_profit"), prev.get("revenue")),
        "ttm_net_profit": ttm(norm, "net_profit"),
        "ttm_revenue": ttm(norm, "revenue"),
        "capital": cur.get("capital"),
    }
    if out["gross_margin"] is not None and out["gross_margin_prior"] is not None:
        out["gross_margin_change"] = out["gross_margin"] - out["gross_margin_prior"]
    else:
        out["gross_margin_change"] = None
    return out


def balance_features(norm: dict[str, Any] | None) -> dict[str, Any]:
    if not norm:
        return {}
    cur, prev = norm.get("current") or {}, norm.get("prior_same_period") or {}

    def debt(d: dict[str, Any]) -> float | None:
        a, b = d.get("short_term_debt"), d.get("long_term_debt")
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    return {
        "total_assets": cur.get("total_assets"),
        "total_liabilities": cur.get("total_liabilities"),
        "equity": cur.get("equity"),
        "debt": debt(cur),
        "debt_growth": growth(debt(cur), debt(prev)),
        "liabilities_growth": growth(cur.get("total_liabilities"), prev.get("total_liabilities")),
        "assets_growth": growth(cur.get("total_assets"), prev.get("total_assets")),
        "debt_to_equity": ratio(cur.get("total_liabilities"), cur.get("equity")),
        "current_ratio": ratio(cur.get("current_assets"), cur.get("current_liabilities")),
        "cash": cur.get("cash"),
    }


def cash_flow_features(norm: dict[str, Any] | None, net_profit: float | None) -> dict[str, Any]:
    if not norm:
        return {}
    cur, prev = norm.get("current") or {}, norm.get("prior_same_period") or {}
    ocf = cur.get("operating_cash_flow")
    return {
        "operating_cash_flow": ocf,
        "operating_cash_flow_growth": growth(ocf, prev.get("operating_cash_flow")),
        "ocf_to_net_profit": ratio(ocf, net_profit) if net_profit and net_profit > 0 else None,
        "cash_flow_trend": (None if ocf is None or prev.get("operating_cash_flow") is None
                            else ("improving" if ocf > prev["operating_cash_flow"] else "weakening")),
    }


def monthly_features(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """`reports`: [{period_end: date, month_sales, ytd_sales, prior_ytd_sales}] newest first,
    one per month (corrections already de-duplicated)."""
    if not reports:
        return {}
    latest = reports[0]
    by_month = {(r["period_end"].year, r["period_end"].month): r for r in reports}
    pe: date = latest["period_end"]
    same_month_last_year = by_month.get((pe.year - 1, pe.month))
    out: dict[str, Any] = {
        "monthly_period_end": pe.isoformat(),
        "month_sales": latest.get("month_sales"),
        "ytd_sales_growth": growth(latest.get("ytd_sales"), latest.get("prior_ytd_sales")),
        "monthly_sales_yoy": growth(latest.get("month_sales"),
                                    same_month_last_year.get("month_sales") if same_month_last_year else None),
    }
    prev3 = [r.get("month_sales") for r in reports[1:4] if r.get("month_sales") is not None]
    out["monthly_sales_vs_prev3"] = growth(latest.get("month_sales"), sum(prev3) / len(prev3)) if len(prev3) == 3 else None
    last3 = [r.get("month_sales") for r in reports[:3]]
    prior_q = [by_month.get((r["period_end"].year - 1, r["period_end"].month)) for r in reports[:3]]
    if all(x is not None for x in last3) and all(p and p.get("month_sales") is not None for p in prior_q):
        out["quarterly_sales_growth"] = growth(sum(last3), sum(p["month_sales"] for p in prior_q))
    else:
        out["quarterly_sales_growth"] = None
    return out


def event_features(letters: list[dict[str, Any]], asof: datetime, lookback_days: int = 30) -> dict[str, Any]:
    """Flags from disclosures published in (asof - lookback, asof]."""
    start = asof - timedelta(days=lookback_days)
    recent = [L for L in letters if L["published_at"] and start < L["published_at"] <= asof]
    tags = {t for L in recent for t in (L.get("event_tags") or [])}
    types = {L["report_type"] for L in recent}
    return {
        "recent_letters": len(recent),
        "has_major_disclosure": "major_disclosure" in types or "major_disclosure" in tags,
        "has_capital_increase": "capital_increase" in tags or "capital_increase" in types,
        "has_major_contract": "major_contract" in tags,
        "has_auditor_issue": "auditor_issue" in tags,
        "has_trading_halt": bool({"halt", "suspension"} & tags),
        "has_clarification": "clarification" in types or "price_fluctuation" in tags,
        "recent_titles": [
            {"title": L["title"], "type": L["report_type"], "published_at": L["published_at"].isoformat()}
            for L in sorted(recent, key=lambda x: x["published_at"], reverse=True)[:5]
        ],
    }


def report_direction(feat: dict[str, Any], hurdle: float = 0.30) -> dict[str, bool]:
    """Positive/negative latest report.

    Growth is nominal while Iranian inflation is ~30-40%/yr, so "positive" means growth above an
    inflation-like `hurdle` and "negative" means below zero (a real decline). Profit counts double,
    and a loss-making period can never be classified positive.
    """
    weighted = [(feat.get("profit_growth"), 2), (feat.get("revenue_growth"), 1),
                (feat.get("ytd_sales_growth"), 1), (feat.get("monthly_sales_yoy"), 1)]
    weighted = [(g, w) for g, w in weighted if g is not None]
    if not weighted:
        return {"recent_report_positive": False, "recent_report_negative": False}
    pos = sum(w for g, w in weighted if g > hurdle)
    neg = sum(w for g, w in weighted if g < 0)
    loss = feat.get("net_profit") is not None and feat["net_profit"] < 0
    return {"recent_report_positive": pos > neg and not loss, "recent_report_negative": neg > pos or
            (loss and (feat.get("profit_growth") or 0) < 0)}


def valuation_features(market_cap_rial: float | None, feat: dict[str, Any]) -> dict[str, Any]:
    ttm_np, ttm_rev, equity = feat.get("ttm_net_profit"), feat.get("ttm_revenue"), feat.get("equity")
    return {
        "market_cap": market_cap_rial,
        "pe_ttm": ratio(market_cap_rial, ttm_np * MILLION) if ttm_np and ttm_np > 0 else None,
        "ps_ttm": ratio(market_cap_rial, ttm_rev * MILLION) if ttm_rev else None,
        "pb": ratio(market_cap_rial, equity * MILLION) if equity and equity > 0 else None,
    }


# --------------------------------------------------------------------------- DB-backed assembly


def _latest(s, symbol: str, sheet: str, asof: datetime) -> FinancialStatement | None:
    return s.execute(
        select(FinancialStatement)
        .where(FinancialStatement.symbol == symbol, FinancialStatement.sheet == sheet,
               FinancialStatement.published_at <= asof)
        .order_by(FinancialStatement.period_end.desc(), FinancialStatement.published_at.desc())
        .limit(1)
    ).scalars().first()


def fundamental_features_for(
    symbol: str, asof: datetime, market_cap_rial: float | None = None, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg = cfg or {}
    symbol = normalize_fa(symbol)
    with session_scope() as s:
        inc = _latest(s, symbol, "income", asof)
        bal = _latest(s, symbol, "balance_sheet", asof)
        cf = _latest(s, symbol, "cash_flow", asof)
        monthly_rows = s.execute(
            select(FinancialStatement)
            .where(FinancialStatement.symbol == symbol, FinancialStatement.sheet == "monthly_sales",
                   FinancialStatement.published_at <= asof)
            .order_by(FinancialStatement.period_end.desc(), FinancialStatement.published_at.desc())
        ).scalars().all()
        letters = [
            {"title": L.title, "report_type": L.report_type, "event_tags": L.event_tags,
             "published_at": L.published_at}
            for L in s.execute(
                select(CodalLetter).where(CodalLetter.symbol == symbol, CodalLetter.published_at <= asof,
                                          CodalLetter.published_at > asof - timedelta(days=120))
            ).scalars()
        ]

    # latest correction wins for the same month
    monthly: dict[date, dict[str, Any]] = {}
    for r in monthly_rows:
        if r.period_end not in monthly and r.normalized_data:
            monthly[r.period_end] = {"period_end": r.period_end, **r.normalized_data}
    monthly_list = sorted(monthly.values(), key=lambda x: x["period_end"], reverse=True)

    feat: dict[str, Any] = {}
    feat.update(income_features(inc.normalized_data if inc else None))
    feat.update(balance_features(bal.normalized_data if bal else None))
    feat.update(cash_flow_features(cf.normalized_data if cf else None, feat.get("net_profit")))
    feat.update(monthly_features(monthly_list))
    feat.update(event_features(letters, asof, cfg.get("event_lookback_days", 30)))
    feat.update(report_direction(feat, cfg.get("nominal_growth_hurdle", 0.30)))
    feat.update(valuation_features(market_cap_rial, feat))

    feat["data_quality"] = {
        "income_statement": "ok" if inc else "unavailable",
        "balance_sheet": "ok" if bal else "unavailable",
        "cash_flow": "ok" if cf else "unavailable",
        "monthly_activity": "ok" if monthly_list else "unavailable",
        "statement_published_at": inc.published_at.isoformat() if inc and inc.published_at else None,
        "statement_audited": inc.audited if inc else None,
        "source": "codal",
    }
    return feat
