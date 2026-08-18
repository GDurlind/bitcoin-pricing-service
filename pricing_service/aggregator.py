"""Median + MAD consensus pricing. Pure functions, no I/O."""

from dataclasses import dataclass
from enum import StrEnum
from statistics import median

from pricing_service.models import PriceQuote

# 1.4826 makes MAD a consistent estimator of standard deviation under normality,
# so the modified z-score below is comparable to an ordinary z-score cutoff.
MAD_SCALE = 1.4826
OUTLIER_THRESHOLD = 3.5  # Iglewicz & Hoaglin's recommended modified z-score cutoff
MIN_SOURCES_FOR_REJECTION = 3


class QuoteStatus(StrEnum):
    USED = "used"
    OUTLIER = "outlier"


class NoQuotesError(ValueError):
    """Raised when consensus is requested with zero live quotes."""


@dataclass(frozen=True, slots=True)
class SourceBreakdown:
    """One source's quote as it factored into the consensus."""

    source: str
    price: float
    latency_ms: float
    status: QuoteStatus
    deviation_bps: float


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    price: float
    breakdown: list[SourceBreakdown]


def consensus(quotes: list[PriceQuote]) -> ConsensusResult:
    """Compute a consensus price from live quotes.

    Below MIN_SOURCES_FOR_REJECTION, outlier rejection is skipped entirely —
    with too few points MAD is not a meaningful measure of spread, and every
    quote is treated as a survivor.
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
    if consensus_price == 0:
        return 0.0
    return (price - consensus_price) / consensus_price * 10_000
