"""Aggregation tests: pure maths on constructed quotes, no network.

Note: "some sources failed" isn't tested directly here — failures are
filtered out by models.partition() before quotes ever reach consensus(),
so from the aggregator's point of view a partial failure just looks like
fewer live quotes. See the 1- and 2-source tests below for that case.
"""

from datetime import UTC, datetime

import pytest

from pricing_service.aggregator import NoQuotesError, QuoteStatus, consensus
from pricing_service.models import PriceQuote


def make_quote(source: str, price: float, latency_ms: float = 50.0) -> PriceQuote:
    return PriceQuote(source, price, latency_ms, datetime.now(UTC))


def statuses_by_source(breakdown) -> dict[str, QuoteStatus]:
    return {b.source: b.status for b in breakdown}


def test_all_sources_agree():
    quotes = [make_quote(s, 67_000.0) for s in ("a", "b", "c", "d")]

    result = consensus(quotes)

    assert result.price == pytest.approx(67_000.0)
    statuses = statuses_by_source(result.breakdown).values()
    assert all(status == QuoteStatus.USED for status in statuses)


def test_single_outlier_is_excluded_and_does_not_move_consensus():
    quotes = [
        make_quote("a", 67_000.0),
        make_quote("b", 67_010.0),
        make_quote("c", 67_005.0),
        make_quote("d", 90_000.0),  # way off — should be rejected
    ]

    result = consensus(quotes)
    statuses = statuses_by_source(result.breakdown)

    assert statuses["d"] == QuoteStatus.OUTLIER
    assert statuses["a"] == statuses["b"] == statuses["c"] == QuoteStatus.USED
    # consensus should sit near the clustered sources, nowhere near the outlier
    assert result.price == pytest.approx(67_005.0, abs=10.0)


def test_two_live_sources_skips_rejection():
    quotes = [make_quote("a", 100.0), make_quote("b", 200.0)]

    result = consensus(quotes)

    assert result.price == pytest.approx(150.0)
    statuses = statuses_by_source(result.breakdown).values()
    assert all(status == QuoteStatus.USED for status in statuses)


def test_single_live_source_is_returned_as_is():
    result = consensus([make_quote("a", 12_345.0)])

    assert result.price == pytest.approx(12_345.0)
    assert result.breakdown[0].status == QuoteStatus.USED


def test_zero_live_sources_raises():
    with pytest.raises(NoQuotesError):
        consensus([])


def test_all_quotes_identical_mad_zero_does_not_reject_anything():
    quotes = [make_quote(s, 500.0) for s in ("a", "b", "c", "d")]

    result = consensus(quotes)

    assert result.price == pytest.approx(500.0)
    statuses = statuses_by_source(result.breakdown).values()
    assert all(status == QuoteStatus.USED for status in statuses)


@pytest.mark.parametrize(
    "prices",
    [
        [10.0, 20.0, 30.0, 1_000_000.0],
        [1.0, 1.0, 1.0, 1_000_000_000.0],
        [50_000.0, 50_001.0, 999_999.0, 1_000_000.0],
    ],
)
def test_never_excludes_every_source(prices):
    quotes = [make_quote(f"s{i}", p) for i, p in enumerate(prices)]

    result = consensus(quotes)

    used = [b for b in result.breakdown if b.status == QuoteStatus.USED]
    assert len(used) >= 1
