"""Median + MAD consensus pricing. Pure functions, no I/O.

The median is used as the base statistic rather than the mean: the mean has a
0% breakdown point (one bad tick can drag it arbitrarily far), while the
median tolerates up to 50% bad inputs. Outliers are then rejected by median
absolute deviation (MAD) rather than standard deviation, because using stdev
to catch the very outlier that inflates that stdev is circular.

Everything in this module is synchronous and does no I/O by design — it's
pure in-memory arithmetic on already-fetched quotes, so there's nothing here
that benefits from being async (see sources.py and main.py for the async
fan-out that produces the `quotes` this module consumes).
"""

from dataclasses import dataclass
from enum import StrEnum
from statistics import median

from pricing_service.models import PriceQuote

# 1.4826 makes MAD a consistent estimator of standard deviation under normality,
# so the modified z-score below is comparable to an ordinary z-score cutoff.
MAD_SCALE = 1.4826
OUTLIER_THRESHOLD = 3.5  # Iglewicz & Hoaglin's recommended modified z-score cutoff
MIN_SOURCES_FOR_REJECTION = 3  # below this, MAD isn't a meaningful measure of spread


class QuoteStatus(StrEnum):
    """Whether a live quote factored into the consensus price.

    Attributes:
        USED: The quote survived rejection and contributed to the price.
        OUTLIER: The quote was rejected as statistically inconsistent with
            the rest and excluded from the price (but still reported).
    """

    USED = "used"
    OUTLIER = "outlier"


class NoQuotesError(ValueError):
    """Raised when consensus is requested with zero live quotes.

    Callers are expected to check for an empty quote list themselves before
    calling consensus() — main.py does exactly that, falling back to a cached
    result or a 503 — so in practice this is a defensive guard rather than a
    commonly-handled exception.
    """


@dataclass(frozen=True, slots=True)
class SourceBreakdown:
    """One source's quote as it factored into the consensus.

    Attributes:
        source: Exchange name.
        price: The quote's own price (unchanged from the input PriceQuote).
        latency_ms: How long this source took to respond.
        status: Whether this quote was used or rejected as an outlier.
        deviation_bps: How far this quote sits from the final consensus
            price, in basis points (positive = above consensus, negative =
            below). Computed against the *final* price, so an excluded
            outlier's deviation reflects how far it was from the number that
            was actually decided on.
    """

    source: str
    price: float
    latency_ms: float
    status: QuoteStatus
    deviation_bps: float


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    """The output of consensus(): one price, plus the full evidence for it.

    Attributes:
        price: The consensus BTC-USD price.
        breakdown: Every live quote's SourceBreakdown, both used and
            rejected, in the same order the quotes were supplied.
    """

    price: float
    breakdown: list[SourceBreakdown]


def consensus(quotes: list[PriceQuote]) -> ConsensusResult:
    """Compute a consensus price from live quotes.

    Below MIN_SOURCES_FOR_REJECTION, outlier rejection is skipped entirely —
    with too few points MAD is not a meaningful measure of spread, and every
    quote is treated as a survivor. For 1 or 2 quotes, the mean of that
    untouched set is mathematically identical to the plain median, so this
    also satisfies "fall back to plain median" with no separate branch.

    For MIN_SOURCES_FOR_REJECTION or more quotes, the final price is the mean
    of the survivors after outlier rejection (see _reject_outliers) — not the
    median again — so it uses the information from every remaining source
    rather than just the middle one.

    Args:
        quotes: Live price quotes from this poll. Must be non-empty.

    Returns:
        A ConsensusResult with the final price and a per-source breakdown.

    Raises:
        NoQuotesError: If `quotes` is empty.
    """
    if not quotes:
        raise NoQuotesError("cannot compute consensus from zero quotes")

    prices = [q.price for q in quotes]
    base_median = median(prices)

    if len(quotes) < MIN_SOURCES_FOR_REJECTION:
        survivors = quotes
    else:
        survivors = _reject_outliers(quotes, base_median)

    consensus_price = sum(q.price for q in survivors) / len(survivors)
    survivor_sources = {q.source for q in survivors}

    breakdown = [
        SourceBreakdown(
            source=q.source,
            price=q.price,
            latency_ms=q.latency_ms,
            status=QuoteStatus.USED if q.source in survivor_sources else QuoteStatus.OUTLIER,
            deviation_bps=_deviation_bps(q.price, consensus_price),
        )
        for q in quotes
    ]
    return ConsensusResult(price=consensus_price, breakdown=breakdown)


def _reject_outliers(quotes: list[PriceQuote], base_median: float) -> list[PriceQuote]:
    """Drop quotes whose modified z-score exceeds OUTLIER_THRESHOLD.

    Never returns an empty list: the quote(s) sitting exactly at the median
    always score a modified z-score of 0 (always <= OUTLIER_THRESHOLD), so at
    least one quote survives by construction. The final `or quotes` fallback
    is defence-in-depth for that guarantee, not a reachable branch.

    Args:
        quotes: The full set of live quotes (length >= MIN_SOURCES_FOR_REJECTION).
        base_median: The plain median of all quotes' prices, used as the
            center point deviations are measured from.

    Returns:
        The subset of `quotes` that passed the outlier check — or, if MAD is
        0 (every quote identical) or nothing passes, the full input unchanged.
    """
    deviations = [abs(q.price - base_median) for q in quotes]
    mad = median(deviations)

    if mad == 0:
        # Every quote sits at the median: nothing to reject, and dividing by
        # a zero MAD would be undefined.
        return quotes

    survivors = [
        q
        for q, dev in zip(quotes, deviations, strict=True)
        if (dev / mad) * MAD_SCALE <= OUTLIER_THRESHOLD
    ]
    # The quote(s) at/near the median always have a small modified z-score, so
    # this should never be empty — but never exclude every source, on principle.
    return survivors if survivors else quotes


def _deviation_bps(price: float, consensus_price: float) -> float:
    """How far `price` sits from `consensus_price`, in basis points.

    Args:
        price: A single quote's price.
        consensus_price: The final consensus price to measure against.

    Returns:
        Signed deviation in bps (positive = above consensus). 0.0 if
        consensus_price is 0, to avoid a division by zero that can't
        meaningfully occur with real BTC prices anyway.
    """
    if consensus_price == 0:
        return 0.0
    return (price - consensus_price) / consensus_price * 10_000
