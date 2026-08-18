"""Endpoint tests. fetch_all/fetch_gbp_rate are mocked, so nothing here
touches the network.

Patched at pricing_service.main.fetch_all / fetch_gbp_rate — main.py
imported those names into its own namespace, so patching
pricing_service.sources.* instead would silently miss (main would still
call its own already-bound reference).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from pricing_service.main import app
from pricing_service.models import FailureKind, PriceQuote, SourceFailure


def make_quote(source: str, price: float, latency_ms: float = 50.0) -> PriceQuote:
    return PriceQuote(source, price, latency_ms, datetime.now(UTC))


def make_failure(
    source: str, kind: FailureKind = FailureKind.NETWORK_ERROR, detail: str = "boom"
) -> SourceFailure:
    return SourceFailure(source, kind, detail, 10.0)


@pytest.fixture
def client():
    with TestClient(app) as c:
        # app.state persists across tests on this shared app object; reset the
        # caches so tests don't depend on execution order.
        app.state.last_good = None
        app.state.last_good_fx = None
        yield c


@pytest.fixture(autouse=True)
def default_fx():
    # Every test gets a working FX rate unless it overrides this patch
    # itself — otherwise every test not focused on FX would need its own
    # boilerplate mock just to avoid a real network call.
    with patch("pricing_service.main.fetch_gbp_rate", new=AsyncMock(return_value=0.79)):
        yield


def _mock_fetch_all(results):
    return patch("pricing_service.main.fetch_all", new=AsyncMock(return_value=results))


def _mock_fetch_fx(rate: float | None):
    return patch("pricing_service.main.fetch_gbp_rate", new=AsyncMock(return_value=rate))


def test_get_price_all_sources_live(client):
    quotes = [make_quote(s, 67_000.0) for s in ("a", "b", "c", "d")]

    with _mock_fetch_all(quotes):
        response = client.get("/api/price")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "live"
    assert body["price"] == pytest.approx(67_000.0)
    assert [s["status"] for s in body["sources"]] == ["used"] * 4


def test_get_price_degraded_when_a_source_fails(client):
    results = [
        make_quote("a", 67_000.0),
        make_quote("b", 67_010.0),
        make_quote("c", 67_005.0),
        make_failure("d", FailureKind.TIMEOUT, "no response within 3.0s"),
    ]

    with _mock_fetch_all(results):
        response = client.get("/api/price")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"

    by_source = {s["source"]: s for s in body["sources"]}
    assert by_source["d"]["status"] == "failed"
    assert by_source["d"]["price"] is None
    assert by_source["d"]["detail"] == "no response within 3.0s"
    assert by_source["a"]["status"] == "used"


def test_get_price_flags_outlier_in_breakdown(client):
    quotes = [
        make_quote("a", 67_000.0),
        make_quote("b", 67_010.0),
        make_quote("c", 67_005.0),
        make_quote("d", 90_000.0),  # far off — should be rejected
    ]

    with _mock_fetch_all(quotes):
        response = client.get("/api/price")

    body = response.json()
    by_source = {s["source"]: s for s in body["sources"]}
    assert by_source["d"]["status"] == "outlier"
    assert by_source["d"]["price"] == pytest.approx(90_000.0)
    # the outlier's own presence shouldn't drag the consensus toward it
    assert body["price"] == pytest.approx(67_005.0, abs=10.0)


def test_get_price_cold_start_total_failure_returns_503(client):
    failures = [make_failure(s) for s in ("a", "b", "c", "d")]

    with _mock_fetch_all(failures):
        response = client.get("/api/price")

    assert response.status_code == 503


def test_get_price_falls_back_to_stale_cache_on_total_failure(client):
    quotes = [make_quote(s, 67_000.0) for s in ("a", "b", "c", "d")]
    with _mock_fetch_all(quotes):
        live_response = client.get("/api/price").json()

    failures = [make_failure(s) for s in ("a", "b", "c", "d")]
    with _mock_fetch_all(failures):
        stale_response = client.get("/api/price")

    assert stale_response.status_code == 200
    body = stale_response.json()
    assert body["status"] == "stale"
    # a stale response replays the last good result rather than reconstructing
    # one, so both the price and the original timestamp carry over unchanged.
    assert body["price"] == pytest.approx(live_response["price"])
    assert body["as_of"] == live_response["as_of"]


def test_dashboard_serves_index_html(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_get_price_includes_gbp_rate_when_fx_available(client):
    quotes = [make_quote(s, 67_000.0) for s in ("a", "b", "c", "d")]

    with _mock_fetch_all(quotes), _mock_fetch_fx(0.79):
        response = client.get("/api/price")

    assert response.json()["fx"] == {"gbp_rate": pytest.approx(0.79)}


def test_get_price_fx_null_when_unavailable_and_never_cached(client):
    quotes = [make_quote(s, 67_000.0) for s in ("a", "b", "c", "d")]

    with _mock_fetch_all(quotes), _mock_fetch_fx(None):
        response = client.get("/api/price")

    assert response.json()["fx"] is None


def test_get_price_reuses_cached_fx_when_fx_fails_but_btc_succeeds(client):
    quotes = [make_quote(s, 67_000.0) for s in ("a", "b", "c", "d")]

    with _mock_fetch_all(quotes), _mock_fetch_fx(0.79):
        client.get("/api/price")  # populate the fx cache

    with _mock_fetch_all(quotes), _mock_fetch_fx(None):
        response = client.get("/api/price")

    # BTC sources are fine this poll, so the response is fresh ("live"), but
    # the FX rate itself is reused from the last time it succeeded.
    body = response.json()
    assert body["status"] == "live"
    assert body["fx"] == {"gbp_rate": pytest.approx(0.79)}
