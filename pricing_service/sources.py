"""Independent async fetchers, one per exchange.

Each fetcher shares one httpx.AsyncClient (owned by main.py) but gets its own
timeout budget. Every fetcher is guaranteed to return a SourceResult and never
raise, so a slow or broken source can never take down the others.
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
    return float(data["data"]["amount"])


def _parse_kraken(data: Any) -> float:
    if data.get("error"):
        raise ValueError(f"kraken error: {data['error']}")
    ticker = next(iter(data["result"].values()))
    return float(ticker["c"][0])


def _parse_binance(data: Any) -> float:
    return float(data["price"])


def _parse_bitstamp(data: Any) -> float:
    return float(data["last"])


@dataclass(frozen=True, slots=True)
class SourceConfig:
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
    """Fetch every source concurrently. A slow or failing source never blocks the rest."""
    return list(
        await asyncio.gather(
            *(_fetch_quote(client, s.name, s.url, s.timeout, s.parse) for s in SOURCES)
        )
    )
