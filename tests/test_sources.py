"""Source-fetcher tests. No real network access — httpx.MockTransport fakes it.

Parsers and _fetch_quote are private, but they're where the actual
branching logic lives (JSON shape, exception -> FailureKind mapping), so
they're tested directly rather than only through the public fetch_all.
"""

import httpx
import pytest

from pricing_service.models import FailureKind, PriceQuote, SourceFailure
from pricing_service.sources import (
    SOURCES,
    _fetch_quote,
    _parse_binance,
    _parse_bitstamp,
    _parse_coinbase,
    _parse_kraken,
    fetch_all,
)

# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def test_parse_coinbase():
    assert _parse_coinbase({"data": {"amount": "64993.475", "base": "BTC"}}) == pytest.approx(
        64_993.475
    )


def test_parse_kraken():
    payload = {"error": [], "result": {"XXBTZUSD": {"c": ["64955.80000", "0.001"]}}}
    assert _parse_kraken(payload) == pytest.approx(64_955.80)


def test_parse_kraken_raises_on_api_error():
    payload = {"error": ["EQuery:Unknown asset pair"], "result": {}}
    with pytest.raises(ValueError, match="kraken error"):
        _parse_kraken(payload)


def test_parse_binance():
    assert _parse_binance({"symbol": "BTCUSDT", "price": "64995.72000000"}) == pytest.approx(
        64_995.72
    )


def test_parse_bitstamp():
    assert _parse_bitstamp({"last": "64942.62"}) == pytest.approx(64_942.62)


# --------------------------------------------------------------------------
# _fetch_quote: one test per branch of the exception -> FailureKind mapping
# --------------------------------------------------------------------------


async def test_fetch_quote_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"price": "64995.72"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _fetch_quote(client, "test", "https://example.test/", 3.0, _parse_binance)

    assert isinstance(result, PriceQuote)
    assert result.source == "test"
    assert result.price == pytest.approx(64_995.72)
    assert result.latency_ms >= 0


async def test_fetch_quote_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _fetch_quote(client, "test", "https://example.test/", 3.0, _parse_binance)

    assert isinstance(result, SourceFailure)
    assert result.kind == FailureKind.HTTP_ERROR
    assert "503" in result.detail


async def test_fetch_quote_bad_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _fetch_quote(client, "test", "https://example.test/", 3.0, _parse_binance)

    assert isinstance(result, SourceFailure)
    assert result.kind == FailureKind.BAD_PAYLOAD


async def test_fetch_quote_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _fetch_quote(client, "test", "https://example.test/", 3.0, _parse_binance)

    assert isinstance(result, SourceFailure)
    assert result.kind == FailureKind.TIMEOUT


async def test_fetch_quote_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _fetch_quote(client, "test", "https://example.test/", 3.0, _parse_binance)

    assert isinstance(result, SourceFailure)
    assert result.kind == FailureKind.NETWORK_ERROR


# --------------------------------------------------------------------------
# fetch_all: fan-out isolation across real source configs
# --------------------------------------------------------------------------


async def test_fetch_all_isolates_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.coinbase.com":
            return httpx.Response(200, json={"data": {"amount": "64993.475"}})
        if host == "www.bitstamp.net":
            return httpx.Response(200, json={"last": "64942.62"})
        if host == "api.kraken.com":
            return httpx.Response(500, json={"error": "boom"})
        if host == "api.binance.com":
            raise httpx.ReadTimeout("timed out", request=request)
        raise AssertionError(f"unexpected host: {host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await fetch_all(client)

    assert len(results) == len(SOURCES)
    by_source = {r.source: r for r in results}

    assert isinstance(by_source["coinbase"], PriceQuote)
    assert isinstance(by_source["bitstamp"], PriceQuote)
    assert isinstance(by_source["kraken"], SourceFailure)
    assert by_source["kraken"].kind == FailureKind.HTTP_ERROR
    assert isinstance(by_source["binance"], SourceFailure)
    assert by_source["binance"].kind == FailureKind.TIMEOUT
