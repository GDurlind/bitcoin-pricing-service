# ----------------------------- Build Stage -----------------------------
FROM python:3.14-slim AS build

WORKDIR /opt/app
ENV POETRY_VIRTUALENVS_IN_PROJECT=true

# Install Poetry and copy only dependency manifests first for better layer caching
RUN pip install --no-cache-dir poetry
COPY pyproject.toml poetry.lock ./

# Install main dependencies only, into ./.venv
RUN poetry install --only main --no-interaction --no-root

# ----------------------------- Runtime Stage -----------------------------

FROM python:3.14-slim AS runtime

WORKDIR /opt/app
ENV PATH="/opt/app/.venv/bin:$PATH"

# Bring in the pre-built venv (no Poetry, build tools, or lockfile in the final image)
COPY --from=build /opt/app/.venv ./.venv

# Copy application source and the static dashboard it serves
COPY ./pricing_service ./pricing_service
COPY ./frontend ./frontend

EXPOSE 8000

# Bind to 0.0.0.0 so the port is reachable from outside the container
CMD ["uvicorn", "pricing_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
