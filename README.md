# bitcoin-pricing-service

A BTC-USD **consensus price** service: it polls several public exchange REST APIs
concurrently and returns a single trustworthy number, plus the evidence behind it.

Rather than averaging whatever comes back, the aggregator takes the median as its base
statistic and rejects outliers by median absolute deviation (MAD) before averaging the
survivors — so one exchange printing a bad tick, or half the sources timing out, degrades
the answer gracefully instead of poisoning it.

- `GET /api/price` — consensus price (USD), a GBP conversion, health status, and a
  per-source breakdown (price, deviation from consensus, latency, used / outlier / failed).
- `/` — a single-page dashboard that polls the endpoint every 2.5s, with a USD/GBP toggle.

## Quickstart

```bash
make init      # pin the Python version via pyenv
make install   # install dependencies with Poetry
make test      # run the test suite (no network required)
make run       # serve the API and dashboard on http://127.0.0.1:8000
make stop      # stop whatever's listening on that port
```

## Design notes

- **Median + MAD, not mean + stdev.** The mean has a 0% breakdown point — one bad tick
  can drag it anywhere. The median tolerates up to 50% bad inputs, and MAD-based
  rejection avoids the circularity of using stdev to catch the outlier that inflates it.
- **Partial failure is the normal case, not an edge case.** Every exchange fetch is
  isolated and allowed to fail independently. The endpoint degrades gracefully
  (`live` → `degraded` → `stale`) rather than failing outright, and falls back to the
  last known-good result if every source fails on a given poll.
- **FX is single-source, deliberately.** Unlike BTC spot, USD/GBP is deep, low-risk
  data that doesn't need cross-source consensus, so it's one lightweight fetcher rather
  than another aggregation pipeline — a display-layer conversion of the same USD price,
  not a second priced asset.
