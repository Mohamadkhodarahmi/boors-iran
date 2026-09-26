from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from bors_analyser.db.models import DailyPrice
from bors_analyser.db.session import upsert


def test_upsert_inserts_then_updates(engine):
    with Session(engine) as s:
        upsert(s, DailyPrice, [{"ins_code": "a", "date": date(2026, 1, 1), "close": 10.0, "source": "x"}])
        upsert(s, DailyPrice, [{"ins_code": "a", "date": date(2026, 1, 1), "close": 11.0, "source": "tsetmc"}])
        s.commit()
        rows = s.execute(select(DailyPrice)).scalars().all()
    assert len(rows) == 1 and rows[0].close == 11.0 and rows[0].source == "tsetmc"


def test_upsert_large_batch_is_chunked(engine):
    rows = [{"ins_code": str(i), "date": date(2026, 1, 1), "close": float(i)} for i in range(5000)]
    with Session(engine) as s:
        assert upsert(s, DailyPrice, rows) == 5000
        s.commit()
        assert len(s.execute(select(DailyPrice.ins_code)).all()) == 5000
