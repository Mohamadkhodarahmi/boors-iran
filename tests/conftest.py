import pytest
from sqlalchemy import create_engine

from bors_analyser.db.models import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", future=True)
    Base.metadata.create_all(eng)
    return eng
