"""Tests for venue HTTP parsers — defensive shape handling.

These tests mock httpx.AsyncClient at the module level so the parsers run
end-to-end without live network access. They lock the "garbage-in →
None-out, never-crash" contract for each venue client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dfm.venues import BackpackClient, HyperliquidClient, OrderlyClient


def _mock_async_client(json_payload, *, status_code: int = 200):
    """Build a context-manager mock that returns a Response with `json_payload`
    on either GET or POST. Mirrors the `async with httpx.AsyncClient(...) as cli`
    pattern used inside `_get_with_retry` and `_post_with_retry`.
    """
    response = MagicMock()
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_payload)

    async_client = MagicMock()
    async_client.get = AsyncMock(return_value=response)
    async_client.post = AsyncMock(return_value=response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=async_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_hl_parser_handles_scalar_response():
    """Hyperliquid /info returning a bare scalar (not the expected 2-list)
    must yield None, not raise."""
    cli = HyperliquidClient()
    for bad_payload in (42, "string", None, {"unexpected": "dict"}):
        with patch("dfm.venues.httpx.AsyncClient",
                   return_value=_mock_async_client(bad_payload)):
            result = await cli.fetch_market_state("SOL-PERP")
        assert result is None, f"scalar payload {bad_payload!r} should yield None"


@pytest.mark.asyncio
async def test_hl_parser_handles_short_list():
    """Hyperliquid returning a 1-element list (missing the ctxs array)
    must yield None."""
    cli = HyperliquidClient()
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client([{"universe": []}])):
        result = await cli.fetch_market_state("SOL-PERP")
    assert result is None


@pytest.mark.asyncio
async def test_hl_parser_handles_zero_mark_price():
    """Hyperliquid returning markPx=0 (data error) must yield None — the
    Round-5 fix that prevents ConcentrationRisk misreading $0 as a real mark.
    """
    cli = HyperliquidClient()
    payload = [
        {"universe": [{"name": "SOL"}]},
        [{"markPx": "0", "oraclePx": "150", "funding": "0.0001",
          "openInterest": "1000"}],
    ]
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client(payload)):
        result = await cli.fetch_market_state("SOL-PERP")
    assert result is None


@pytest.mark.asyncio
async def test_orderly_parser_handles_missing_fields():
    """Orderly returning success=true but data={} (no last_funding_rate)
    must yield a FundingRate with hourly_rate=0, not crash."""
    cli = OrderlyClient()
    payload = {"success": True, "data": {}}
    # Patch BOTH the funding rate fetch and the interval fetch (it'll fall
    # back to the 8h default since the markets endpoint returns nothing useful).
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client(payload)):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is not None
    assert result.hourly_rate == 0.0


@pytest.mark.asyncio
async def test_orderly_parser_handles_list_response():
    """Orderly returning a list instead of dict must yield None."""
    cli = OrderlyClient()
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client([1, 2, 3])):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is None


@pytest.mark.asyncio
async def test_orderly_parser_handles_null_data():
    """Orderly returning success=true but data=null must yield None."""
    cli = OrderlyClient()
    payload = {"success": True, "data": None}
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client(payload)):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is None


@pytest.mark.asyncio
async def test_backpack_parser_handles_list_of_strings():
    """Backpack returning [str, str, ...] (not [dict, dict, ...]) must
    yield None, not crash on .get() against a string element."""
    cli = BackpackClient()
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client(["bad", "data"])):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is None


@pytest.mark.asyncio
async def test_backpack_parser_handles_empty_list():
    """Backpack returning [] must yield None, not IndexError."""
    cli = BackpackClient()
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client([])):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is None


@pytest.mark.asyncio
async def test_backpack_parser_handles_non_list_response():
    """Backpack returning a dict instead of list must yield None."""
    cli = BackpackClient()
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client({"error": "rate limited"})):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is None


@pytest.mark.asyncio
async def test_orderly_interval_cached_per_symbol():
    """The funding-interval fetch is cached per (client, symbol). Two
    successive `fetch_funding_rate` calls for the same symbol must only
    hit the markets/history endpoint once.
    """
    cli = OrderlyClient()
    # Pre-seed cache so we know the cache path is exercised.
    cli._interval_cache["PERP_SOL_USDC"] = 4.0
    payload = {"success": True, "data": {"last_funding_rate": 0.0004}}
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client(payload)):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is not None
    # rate / 4h interval = 0.0001/h
    assert abs(result.hourly_rate - 0.0001) < 1e-12


@pytest.mark.asyncio
async def test_backpack_interval_cached_per_symbol():
    """Same as Orderly but for Backpack. With a 1h interval cached, the
    parser must divide by 1, not by the 8h default.
    """
    cli = BackpackClient()
    cli._interval_cache["SOL_USDC_PERP"] = 1.0
    payload = [{"fundingRate": "0.00005", "symbol": "SOL_USDC_PERP"}]
    with patch("dfm.venues.httpx.AsyncClient",
               return_value=_mock_async_client(payload)):
        result = await cli.fetch_funding_rate("SOL-PERP")
    assert result is not None
    # 0.00005 / 1h = 0.00005/h (not 0.00005/8 = 6.25e-6)
    assert abs(result.hourly_rate - 0.00005) < 1e-12
