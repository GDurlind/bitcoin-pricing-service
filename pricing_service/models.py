"""Normalised shapes every exchange response is converted into."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FailureKind(StrEnum):
    """Why a source produced no usable price."""

    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    BAD_PAYLOAD = "bad_payload"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """A successful BTC-USD spot price from one exchange."""

    source: str
    price: float
    latency_ms: float
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SourceFailure:
    """One exchange that did not produce a usable price."""

    source: str
    kind: FailureKind
    detail: str
    latency_ms: float


type SourceResult = PriceQuote | SourceFailure


def partition(
    results: Iterable[SourceResult],
) -> tuple[list[PriceQuote], list[SourceFailure]]:
    """Split fan-out results into usable quotes and failures."""
    quotes = [r for r in results if isinstance(r, PriceQuote)]
    failures = [r for r in results if isinstance(r, SourceFailure)]
    return quotes, failures
