"""FastAPI app: /api/price plus the static dashboard.

Translates between aggregator.py's plain dataclasses and the pydantic models
FastAPI needs for response serialisation and OpenAPI schema generation — the
one module allowed to know both shapes exist (see models.py's docstring for
why the core stays on plain dataclasses).

Owns the one long-lived httpx.AsyncClient (created in lifespan(), reused by
every poll) and two small caches on app.state: the last successful
PriceResponse, and the last successful GBP rate — each used to degrade
gracefully when a poll partially or fully fails, rather than surfacing
nothing at all.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pricing_service.aggregator import QuoteStatus, SourceBreakdown, consensus
from pricing_service.models import SourceFailure, partition
from pricing_service.sources import fetch_all, fetch_gbp_rate

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class SourceStatus(StrEnum):
    """Per-source status shown in the dashboard's breakdown table.

    Superset of aggregator.QuoteStatus (used/outlier) plus FAILED, which the
    aggregator never sees — failures are filtered out by models.partition()
    before quotes ever reach it. _build_breakdown() is the one place that
    maps between the two vocabularies.
    """

    USED = "used"
    OUTLIER = "outlier"
    FAILED = "failed"


class HealthStatus(StrEnum):
    """Overall response health, recomputed fresh on every poll.

    Attributes:
        LIVE: Zero failures this poll. An outlier rejection alone doesn't
            count against this — that's the aggregator working correctly,
            not something wrong.
        DEGRADED: At least one source failed, but at least one quote still
            came through, so the price is still fresh.
        STALE: Zero live quotes this poll; the response replays the last
            known-good result verbatim (same price, same original as_of)
            rather than reconstructing one.
    """

    LIVE = "live"
    DEGRADED = "degraded"
    STALE = "stale"


class SourceBreakdownOut(BaseModel):
    """API/dashboard shape for one source's row in the breakdown table.

    Attributes:
        source: Exchange name.
        status: used / outlier / failed.
        latency_ms: How long this source took to respond (or to fail).
        price: The source's own price, or None if it failed.
        deviation_bps: Deviation from the consensus price, or None if it
            failed (there's no price to compare).
        detail: Failure detail (e.g. "HTTP 503"), or None if it succeeded.
    """

    source: str
    status: SourceStatus
    latency_ms: float
    price: float | None = None
    deviation_bps: float | None = None
    detail: str | None = None


class FxOut(BaseModel):
    """The USD/GBP rate used for the dashboard's currency toggle.

    Absent (None on the parent PriceResponse) whenever no FX rate has ever
    been successfully fetched yet — the dashboard disables the GBP option
    in that case rather than showing a stale or fabricated rate.
    """

    gbp_rate: float


class PriceResponse(BaseModel):
    """The full /api/price response body.

    Attributes:
        price: The consensus BTC-USD price.
        status: live / degraded / stale — see HealthStatus.
        as_of: When this price was computed. On a stale response, this is
            the *original* timestamp from when the cached result was last
            actually live, not the current time — so the dashboard can show
            genuinely how old the number is.
        sources: Per-source breakdown, used by the dashboard's table.
        fx: The current USD/GBP rate, or None if never successfully fetched.
    """

    price: float
    status: HealthStatus
    as_of: datetime
    sources: list[SourceBreakdownOut]
    fx: FxOut | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage the app's shared state for its whole process lifetime.

    Runs once at startup (everything before `yield`) and once at shutdown
    (everything after). The single httpx.AsyncClient is created here rather
    than per-request specifically so its connection pool is reused across
    every 2.5-second poll instead of paying for a fresh TCP+TLS handshake
    to five APIs on every single request.

    Args:
        app: The FastAPI app instance; state is stored on app.state so
            request handlers can reach it via request.app.state.
    """
    app.state.client = httpx.AsyncClient()
    app.state.last_good = None
    app.state.last_good_fx = None
    yield
    await app.state.client.aclose()


app = FastAPI(title="bitcoin-pricing-service", lifespan=lifespan)


def _build_breakdown(
    quote_breakdown: list[SourceBreakdown], failures: list[SourceFailure]
) -> list[SourceBreakdownOut]:
    """Merge live-quote and failure breakdowns into one API-shaped list.

    The single place the aggregator's status vocabulary (used/outlier) and
    the fetch layer's failure vocabulary (SourceFailure) meet and get
    translated into the dashboard's unified SourceStatus.

    Args:
        quote_breakdown: Per-quote results from aggregator.consensus(),
            covering only sources that actually returned a price.
        failures: Sources that failed this poll, from models.partition().

    Returns:
        One SourceBreakdownOut per source (live or failed), sorted
        alphabetically by source name for a stable dashboard row order.
    """
    used = [
        SourceBreakdownOut(
            source=b.source,
            status=SourceStatus.USED if b.status == QuoteStatus.USED else SourceStatus.OUTLIER,
            price=b.price,
            deviation_bps=b.deviation_bps,
            latency_ms=b.latency_ms,
        )
        for b in quote_breakdown
    ]
    failed = [
        SourceBreakdownOut(
            source=f.source,
            status=SourceStatus.FAILED,
            latency_ms=f.latency_ms,
            detail=f.detail,
        )
        for f in failures
    ]
    return sorted(used + failed, key=lambda s: s.source)


@app.get("/api/price", response_model=PriceResponse)
async def get_price(request: Request) -> PriceResponse:
    """Fetch every source concurrently and return a consensus price.

    One poll = one full fetch-and-aggregate cycle; nothing is pre-computed
    or scheduled in the background. The five external calls (four exchanges
    plus the FX rate) run concurrently via asyncio.gather, so the wall-clock
    cost is roughly the slowest single source, not their sum.

    Falls back to replaying the last known-good response (status="stale")
    if every BTC source fails this poll, or raises 503 if nothing has ever
    succeeded yet. A failed FX fetch alone doesn't affect BTC pricing or
    trigger a fallback — it just reuses the last successful GBP rate.

    Args:
        request: Used to reach the shared httpx.AsyncClient and caches on
            request.app.state (set up in lifespan()).

    Returns:
        The consensus price, health status, per-source breakdown, and
        current FX rate.

    Raises:
        HTTPException: 503 if every source has failed and there is no
            cached result to fall back to.
    """
    client: httpx.AsyncClient = request.app.state.client
    results, gbp_rate = await asyncio.gather(fetch_all(client), fetch_gbp_rate(client))
    quotes, failures = partition(results)

    if not quotes:
        cached: PriceResponse | None = request.app.state.last_good
        if cached is None:
            raise HTTPException(status_code=503, detail="no price sources available yet")
        return cached.model_copy(update={"status": HealthStatus.STALE})

    if gbp_rate is not None:
        request.app.state.last_good_fx = gbp_rate
    else:
        gbp_rate = request.app.state.last_good_fx

    result = consensus(quotes)
    response = PriceResponse(
        price=result.price,
        status=HealthStatus.DEGRADED if failures else HealthStatus.LIVE,
        as_of=datetime.now(UTC),
        sources=_build_breakdown(result.breakdown, failures),
        fx=FxOut(gbp_rate=gbp_rate) if gbp_rate is not None else None,
    )

    request.app.state.last_good = response
    return response


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    """Serve the dashboard's index.html, read fresh from disk on every request.

    No caching: the file is small, this endpoint isn't hot, and reading it
    fresh means an edit to frontend/index.html is visible on the next
    browser load with no server restart needed.
    """
    return FileResponse(FRONTEND_DIR / "index.html")
