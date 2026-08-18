"""FastAPI app: /api/price plus the static dashboard."""

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
from pricing_service.sources import fetch_all

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class SourceStatus(StrEnum):
    USED = "used"
    OUTLIER = "outlier"
    FAILED = "failed"


class HealthStatus(StrEnum):
    LIVE = "live"
    DEGRADED = "degraded"
    STALE = "stale"


class SourceBreakdownOut(BaseModel):
    source: str
    status: SourceStatus
    latency_ms: float
    price: float | None = None
    deviation_bps: float | None = None
    detail: str | None = None


class PriceResponse(BaseModel):
    price: float
    status: HealthStatus
    as_of: datetime
    sources: list[SourceBreakdownOut]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.client = httpx.AsyncClient()
    app.state.last_good = None
    yield
    await app.state.client.aclose()


app = FastAPI(title="bitcoin-pricing-service", lifespan=lifespan)


def _build_breakdown(
    quote_breakdown: list[SourceBreakdown], failures: list[SourceFailure]
) -> list[SourceBreakdownOut]:
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
    client: httpx.AsyncClient = request.app.state.client
    results = await fetch_all(client)
    quotes, failures = partition(results)

    if not quotes:
        cached: PriceResponse | None = request.app.state.last_good
        if cached is None:
            raise HTTPException(status_code=503, detail="no price sources available yet")
        return cached.model_copy(update={"status": HealthStatus.STALE})

    result = consensus(quotes)
    response = PriceResponse(
        price=result.price,
        status=HealthStatus.DEGRADED if failures else HealthStatus.LIVE,
        as_of=datetime.now(UTC),
        sources=_build_breakdown(result.breakdown, failures),
    )

    request.app.state.last_good = response
    return response


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
