"""BTC-USD consensus pricing service.

Polls several public exchange REST APIs for BTC-USD spot price concurrently and
returns a single consensus price plus the evidence behind it, rather than a
naive average.

Submodules:
    models:     The normalisation boundary — PriceQuote / SourceFailure, the two
                shapes every exchange response becomes on the way into this system.
    aggregator: Pure, synchronous median + MAD consensus logic. No I/O.
    sources:    Async fetchers — one per exchange, plus one for the USD/GBP FX rate.
    main:       The FastAPI app: GET /api/price and the dashboard at GET /.
"""
