"""Independent async fetchers, one per exchange, plus one for USD/GBP FX.

Each fetcher shares one httpx.AsyncClient (owned by main.py) but gets its own
timeout budget. Every fetcher is guaranteed to return a value and never raise
— _fetch_quote always returns a SourceResult, fetch_gbp_rate always returns a
float or None — so a slow or broken source can never take down the others,
and asyncio.gather never needs return_exceptions=True.

fetch_all() and fetch_gbp_rate() are called concurrently from main.py via one
outer asyncio.gather, and fetch_all() itself fans out into a second, nested
gather over the four exchanges — see main.get_price() for the call site.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pricing_service.models import FailureKind, PriceQuote, SourceFailure, SourceResult


def _parse_coinbase(data: Any) -> float:
    """Extract the spot price from a Coinbase /v2/prices/BTC-USD/spot response."""
    return float(data["data"]["amount"])


def _parse_kraken(data: Any) -> float:
    """Extract the last-trade price from a Kraken /0/public/Ticker response.

    Kraken can return HTTP 200 with an API-level error in the body (e.g. an
    unknown pair), so `data["error"]` is checked explicitly rather than
    trusting a 200 status alone. Raising here is deliberate: _fetch_quote's
    broad except Exception converts it into a BAD_PAYLOAD failure, exactly as
    it would for a genuinely malformed response.
    """
    if data.get("error"):
        raise ValueError(f"kraken error: {data['error']}")
    ticker = next(iter(data["result"].values()))
    return float(ticker["c"][0])


def _parse_binance(data: Any) -> float:
    """Extract the price from a Binance /api/v3/ticker/price response."""
    return float(data["price"])


def _parse_bitstamp(data: Any) -> float:
    """Extract the last-trade price from a Bitstamp /api/v2/ticker response."""
    return float(data["last"])


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One exchange's fetch configuration.

    Attributes:
        name: Exchange name, used as PriceQuote.source / SourceFailure.source.
        url: The REST endpoint to GET.
        timeout: Per-source timeout budget, in seconds. Deliberately varies
            per exchange — e.g. Kraken's public ticker is measurably slower
            under load, and Binance either responds fast or 451s instantly
            (US-origin IPs are geofenced), so waiting longer for it buys
            nothing.
        parse: A function turning the parsed JSON body into a float price.
    """

    name: str
    url: str
    timeout: float
    parse: Callable[[Any], float]


SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        "coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot", 3.0, _parse_coinbase
    ),
    SourceConfig(
        "kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", 4.0, _parse_kraken
    ),
    SourceConfig(
        "binance",
        "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
        2.5,
        _parse_binance,
    ),
    SourceConfig(
        "bitstamp", "https://www.bitstamp.net/api/v2/ticker/btcusd/", 3.0, _parse_bitstamp
    ),
)


async def _fetch_quote(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    timeout: float,
    parse: Callable[[Any], float],
) -> SourceResult:
    """Fetch and parse one exchange's BTC-USD price. Never raises.

    The only suspension point is `await client.get(...)` — everything else
    (parsing, building the result) is synchronous CPU work on already-fetched
    data. Every failure mode — timeout, bad HTTP status, malformed payload, or
    a network-level error — is caught and converted into a typed
    SourceFailure rather than propagating, so a single broken source can
    never take down the asyncio.gather() in fetch_all().

    Args:
        client: The shared httpx.AsyncClient (owned by main.py's lifespan).
        name: Exchange name, used as the result's `source` field.
        url: The REST endpoint to GET.
        timeout: This source's own timeout budget, in seconds.
        parse: Turns the parsed JSON body into a float price; may raise on
            an unexpected shape, which is caught by the final except clause
            below and reported as BAD_PAYLOAD.

    Returns:
        A PriceQuote on success, or a SourceFailure describing why not.
    """
    start = time.perf_counter()
    try:
        response = await client.get(url, timeout=timeout)
        response.raise_for_status()
        price = parse(response.json())
        return PriceQuote(
            source=name,
            price=price,
            latency_ms=(time.perf_counter() - start) * 1000,
            fetched_at=datetime.now(UTC),
        )
    except httpx.TimeoutException:
        kind, detail = FailureKind.TIMEOUT, f"no response within {timeout}s"
    except httpx.HTTPStatusError as exc:
        kind, detail = FailureKind.HTTP_ERROR, f"HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        kind, detail = FailureKind.NETWORK_ERROR, str(exc) or type(exc).__name__
    except Exception as exc:  # malformed/unexpected payload shape from a live API
        kind, detail = FailureKind.BAD_PAYLOAD, f"{type(exc).__name__}: {exc}"

    return SourceFailure(
        source=name,
        kind=kind,
        detail=detail,
        latency_ms=(time.perf_counter() - start) * 1000,
    )


async def fetch_all(client: httpx.AsyncClient) -> list[SourceResult]:
    """Fetch all four exchanges concurrently via asyncio.gather.

    Every _fetch_quote call is wrapped into its own Task by gather, so all
    four are in flight at once — the wall-clock cost of this call is roughly
    the slowest single source's latency, not the sum of all four. A slow or
    failing source never blocks or affects the others.

    Args:
        client: The shared httpx.AsyncClient to fetch with.

    Returns:
        One SourceResult per entry in SOURCES, in the same order as SOURCES
        (gather preserves argument order regardless of completion order).
    """
    return list(
        await asyncio.gather(
            *(_fetch_quote(client, s.name, s.url, s.timeout, s.parse) for s in SOURCES)
        )
    )


FX_URL = "https://open.er-api.com/v6/latest/USD"
FX_TIMEOUT = 3.0


async def fetch_gbp_rate(client: httpx.AsyncClient) -> float | None:
    """USD -> GBP spot rate.

    Unlike BTC spot, G10 FX is deep, low-manipulation-risk data that doesn't
    need cross-source consensus, so a single reputable source is fine. There's
    no per-source breakdown to explain a failure to, so unlike _fetch_quote
    this just collapses any failure to None rather than a typed SourceFailure.

    Args:
        client: The shared httpx.AsyncClient to fetch with.

    Returns:
        The current USD/GBP rate, or None on any failure (timeout, HTTP
        error, or an unexpected response shape).
    """
    try:
        response = await client.get(FX_URL, timeout=FX_TIMEOUT)
        response.raise_for_status()
        return float(response.json()["rates"]["GBP"])
    except Exception:
        return None
