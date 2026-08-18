# bitcoin-pricing-service

A BTC-USD **consensus price** service: it polls several public exchange REST APIs
concurrently and returns a single trustworthy number, plus the evidence behind it.

Rather than averaging whatever comes back, the aggregator takes the median as its base
statistic and rejects outliers by median absolute deviation (MAD) before averaging the
survivors — so one exchange printing a bad tick, or half the sources timing out, degrades
the answer gracefully instead of poisoning it.

- `GET /api/price` — consensus price, health status, and a per-source breakdown
  (price, deviation from consensus, latency, used / excluded / failed).
- `/` — a single-page dashboard that polls the endpoint every 2.5s.

## Quickstart

```bash
make init      # pin the Python version via pyenv
make install   # install dependencies with Poetry
make test      # run the test suite (no network required)
make run       # serve the API and dashboard on http://127.0.0.1:8000
```
