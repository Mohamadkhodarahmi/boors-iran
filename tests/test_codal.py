from datetime import date

import pytest
from codalpy.models import DataSource

from bors_analyser.collectors.codal import (
    classify_letter,
    extract_datasource,
    normalize_statement,
    parse_monthly_sales,
    parse_number,
)


@pytest.mark.parametrize(
    ("title", "rtype", "tags"),
    [
        ("اطلاعات و صورت‌های مالی میاندوره‌ای  دوره ۳ ماهه منتهی به  ۱۴۰۵/۰۳/۳۱ (حسابرسی نشده)",
         "financial_statements", {"not_audited"}),
        ("صورت‌های مالی تلفیقی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی شده)", "financial_statements",
         {"consolidated", "audited"}),
        ("توضیحات در خصوص اطلاعات و صورت های مالی منتشر شده", "statement_explanation", set()),
        ("افشای اطلاعات بااهمیت - (سایر اطلاعات بااهمیت - صورت های مالی حسابرسی شده 1403 تامین آب)",
         "major_disclosure", {"major_disclosure"}),
        ("گزارش فعالیت ماهانه دوره ۱ ماهه منتهی به  ۱۴۰۵/۰۵/۳۱", "monthly_activity", set()),
        ("تصمیمات مجمع عمومی فوق العاده دوره ۱۲ ماهه منتهی به  ۱۴۰۴/۱۲/۲۹(اصلاحیه)", "assembly",
         {"assembly", "correction"}),
        ("زمانبندی پرداخت سود دوره ۱۲ ماهه منتهی به  ۱۴۰۴/۱۲/۲۹", "dividend", {"dividend"}),
        ("تصمیمات هیئت‌مدیره در خصوص افزایش سرمایه", "capital_increase", {"capital_increase"}),
        ("افشای اطلاعات بااهمیت - انعقاد قرارداد فروش", "major_disclosure", {"major_disclosure", "major_contract"}),
    ],
)
def test_classify_letter(title, rtype, tags):
    c = classify_letter(title)
    assert c["report_type"] == rtype
    assert tags <= set(c["event_tags"])


def test_classify_period_parsing():
    c = classify_letter("اطلاعات و صورت‌های مالی میاندوره‌ای دوره ۹ ماهه منتهی به ۱۴۰۴/۰۹/۳۰ (حسابرسی نشده)")
    assert c["period_months"] == 9 and c["period_end"] == date(2025, 12, 21) and c["audited"] is False
    c = classify_letter("صورت‌های مالی سال مالی منتهی به ۱۴۰۳/۱۲/۳۰ (حسابرسی شده)")
    assert c["period_months"] == 12 and c["audited"] is True


def test_parse_number():
    assert parse_number("-601,508,257") == -601508257
    assert parse_number("(12)") == -12
    assert parse_number("--") is None and parse_number("") is None and parse_number(None) is None


def _cell(row, col, value, pe="", **kw):
    return {
        "metaTableId": 1, "metaTableCode": 1, "address": f"{row}-{col}", "formula": "", "validations": "",
        "financialConcept": None, "cellGroupName": "", "category": 1, "colSpan": 1, "columnCode": col,
        "columnSequence": col, "decimalPlace": 0, "rowCode": row, "rowSequence": row, "rowSpan": 1,
        "rowTypeName": "", "value": value, "valueTypeName": "", "dataTypeName": None,
        "periodEndToDate": pe, "yearEndToDate": "1405/12/29", "isAudited": False, **kw,
    }


def _ds(cells, period=3, pe="1405/03/31", title="صورت سود و زيان"):
    return DataSource.model_validate({
        "title_Fa": "x", "title_En": "x", "subject": None, "dsc": None, "type": 1, "period": period,
        "periodEndToDate": pe, "yearEndToDate": "1405/12/29", "periodExtraDay": 0, "isConsolidated": False,
        "tracingNo": 1, "kind": 1, "isAudited": False, "auditState": 0, "registerDateTime": "",
        "sentDateTime": "", "publishDateTime": "", "state": 1, "isForAuditing": False,
        "sheets": [{"code": 1, "title_Fa": "s", "title_En": "s", "sequence": 1, "isDynamic": False, "tables": [{
            "metaTableId": 1, "title_Fa": title, "title_En": None, "sequence": 1, "sheetCode": 1, "code": 1,
            "description": None, "aliasName": None, "versionNo": "1", "cells": cells}]}],
    })


def test_normalize_income_statement_current_and_prior():
    cells = [
        _cell(1, 1, "شرح"), _cell(1, 2, "دوره منتهي به 1405/03/31", "1405/03/31"),
        _cell(1, 3, "تجديد ارائه شده دوره منتهي به 1404/03/31", "1404/03/31"),
        _cell(1, 4, "تجديد ارائه شده دوره منتهي به 1404/12/29", "1404/12/29"), _cell(1, 5, "درصد تغيير"),
        _cell(2, 2, "حسابرسی نشده", "1405/03/31"), _cell(2, 3, "حسابرسی نشده", "1404/03/31"),
        _cell(2, 4, "حسابرسی شده", "1404/12/29"),
        _cell(4, 1, "درآمدهاي عملياتي"), _cell(4, 2, "834166799", "1405/03/31"),
        _cell(4, 3, "847715717", "1404/03/31"), _cell(4, 4, "3668860246", "1404/12/29"), _cell(4, 5, "-2"),
        _cell(6, 1, "سود(زيان) ناخالص"), _cell(6, 2, "232658542", "1405/03/31"), _cell(6, 3, "265756797", "1404/03/31"),
        _cell(11, 1, "سود(زيان) عملياتى"), _cell(11, 2, "197995135", "1405/03/31"),
        _cell(18, 1, "سود(زيان) خالص عمليات در حال تداوم"), _cell(18, 2, "1", "1405/03/31"),
        _cell(21, 1, "سود(زيان) خالص"), _cell(21, 2, "120356493", "1405/03/31"), _cell(21, 3, "161823705", "1404/03/31"),
        _cell(22, 1, "سود(زيان) پايه هر سهم:"), _cell(22, 2, "", "1405/03/31"),
        _cell(27, 1, "سود(زيان) پايه هر سهم"), _cell(27, 2, "63", "1405/03/31"),
    ]
    norm = normalize_statement(_ds(cells), "income")
    cur, prior = norm["current"], norm["prior_same_period"]
    assert cur["revenue"] == 834166799 and cur["gross_profit"] == 232658542
    assert cur["operating_profit"] == 197995135
    assert cur["net_profit"] == 120356493  # not the "continuing operations" line
    assert cur["eps"] == 63  # header row without value skipped
    assert prior["revenue"] == 847715717 and norm["prior_period_end"] == "1404/03/31"
    assert norm["columns"][0]["audited"] is False and norm["columns"][2]["audited"] is True
    assert norm["unit"] == "million_rial"


def test_monthly_sales_production_template():
    hdr = {3: "از ابتدای سال مالی تا تاریخ 1405/04/31", 7: "اصلاحات",
           10: "از ابتدای سال مالی تا تاریخ 1405/04/31 (اصلاح شده)", 14: "دوره یک ماهه منتهی به 1405/05/31",
           18: "از ابتدای سال مالی تا تاریخ 1405/05/31", 22: "از ابتدای سال مالی تا تاریخ 1404/05/31"}
    cells = [_cell(1, 1, "شرح")] + [_cell(1, c, v) for c, v in hdr.items()]
    cells += [_cell(2, c, "مبلغ فروش (میلیون ریال)") for c in (6, 9, 13, 17, 21, 25)]
    cells += [_cell(2, c, "تعداد فروش") for c in (4, 8, 11, 15, 19, 23)]
    cells += [_cell(30, 1, "جمع")] + [_cell(30, c, v) for c, v in
                                        {6: "1325732097", 13: "1325732097", 17: "544271929", 21: "1870004026",
                                         25: "1341324850"}.items()]
    out = parse_monthly_sales(_ds(cells, period=1, pe="1405/05/31", title="تولید و فروش"))
    assert out["month_sales"] == 544271929
    assert out["ytd_sales"] == 1870004026
    assert out["prior_ytd_sales"] == 1341324850
    assert out["template"] == "production"


def test_extract_datasource_absent():
    assert extract_datasource("<html>no data</html>") is None
