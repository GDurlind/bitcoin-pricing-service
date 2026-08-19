"""Normalised shapes every exchange response is converted into.

Every exchange returns a different JSON shape and a different type for the
price (string vs float). The moment a response crosses into this system it
becomes one of exactly two shapes — PriceQuote or SourceFailure — so nothing
downstream ever branches on which exchange a value came from.

Plain, frozen dataclasses are used here deliberately rather than pydantic
models: these are internal domain objects that no untrusted input reaches
directly, so pydantic's validation cost buys nothing. Pydantic is reserved for
main.py's response models, at the actual API boundary.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FailureKind(StrEnum):
    """Why a source produced no usable price.

    Attributes:
        TIMEOUT: No response within the source's own timeout budget.
        HTTP_ERROR: The exchange returned a non-2xx HTTP status.
        BAD_PAYLOAD: A 200 OK response whose JSON didn't parse into the
            expected shape (e.g. a missing key, or an unexpected type).
        NETWORK_ERROR: A connection-level failure — DNS, refused, reset —
            before any HTTP response was even received.
    """

    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    BAD_PAYLOAD = "bad_payload"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """A successful BTC-USD spot price from one exchange.

    Frozen and slotted: a quote is an observation of a moment, so mutating one
    after the fact would always be a bug, and slots turn a typo'd attribute
    assignment into an immediate AttributeError instead of a silent new field.

    Attributes:
        source: Exchange name, e.g. "coinbase".
        price: The normalised spot price, as a float.
        latency_ms: Round-trip time for this fetch, in milliseconds.
        fetched_at: When *this system* received the response. Exchange
            timestamps disagree on what they even mean (last trade time vs
            server time vs absent entirely), so this is the number that
            actually reflects how fresh the quote is.
    """

    source: str
    price: float
    latency_ms: float
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SourceFailure:
    """One exchange that did not produce a usable price.

    Attributes:
        source: Exchange name.
        kind: Which category of failure this was — see FailureKind.
        detail: Human-readable detail for display/debugging, e.g. "HTTP 503"
            or "no response within 3.0s".
        latency_ms: Time spent before failing. A timeout that burned 3000ms is
            meaningfully different from one that failed after 10ms, so this is
            tracked on failures too, not just successes.
    """

    source: str
    kind: FailureKind
    detail: str
    latency_ms: float


type SourceResult = PriceQuote | SourceFailure
"""What one source's fetch produces: either a usable quote, or why it isn't one."""


def partition(
    results: Iterable[SourceResult],
) -> tuple[list[PriceQuote], list[SourceFailure]]:
    """Split fan-out results into usable quotes and failures.

    This is the single place the SourceResult union is ever inspected. Once
    split, everything downstream works on a plain list[PriceQuote] — the type
    itself is the guarantee that every element has a real price, so the
    aggregator that consumes it needs zero failure-handling code of its own.

    Args:
        results: The results of fetching every source, in any order — as
            returned by sources.fetch_all().

    Returns:
        A (quotes, failures) tuple. Either list may be empty; a totally
        successful poll gives an empty failures list, and a totally failed
        poll gives an empty quotes list.
    """
    quotes = [r for r in results if isinstance(r, PriceQuote)]
    failures = [r for r in results if isinstance(r, SourceFailure)]
    return quotes, failures
