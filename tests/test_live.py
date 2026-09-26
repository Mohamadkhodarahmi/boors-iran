"""Live smoke tests against TSETMC/CODAL. Run explicitly: `uv run pytest -m live`."""

import pytest

pytestmark = pytest.mark.live


async def test_tsetmc_market_watch_live():
    from bors_analyser.collectors.tsetmc import fetch_market_watch, tsetmc_session

    async with tsetmc_session():
        prices, ct, state = await fetch_market_watch()
    assert prices.height > 1000
    assert {"pc", "pl", "tvol", "pd1", "qo1"} <= set(prices.columns)
    assert ct is not None and ct.height > 500


async def test_tsetmc_sector_names_live():
    from bors_analyser.collectors.tsetmc import fetch_sector_names

    sectors = await fetch_sector_names()
    assert sectors.get("27") == "فلزات اساسی"
