
.EXPORT_ALL_VARIABLES:
.DEFAULT_GOAL := help
# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
REPO_NAME       ?= bitcoin-pricing-service
PYTHON_VERSION  ?= 3.14.6

HOST            ?= 127.0.0.1
PORT            ?= 8000

# --------------------------------------------------------------------------
# Help
# --------------------------------------------------------------------------
help: ## List available commands
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"}; {printf "  %-16s %s\n", $$1, $$2}'

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
init: ## Install the pinned Python version via pyenv and pin it for this repo
	pyenv install $(PYTHON_VERSION) --skip-existing
	pyenv local $(PYTHON_VERSION)

install: ## Install project dependencies with Poetry
	poetry install

lock: ## Refresh poetry.lock
	poetry lock

# --------------------------------------------------------------------------
# Test / Lint
# --------------------------------------------------------------------------
test: ## Run the test suite (no network access required)
	poetry run pytest

lint: ## Run ruff lint checks
	poetry run ruff check .

format: ## Auto-format source with ruff
	poetry run ruff format .

# --------------------------------------------------------------------------
# Run locally
# --------------------------------------------------------------------------
run: ## Serve the API and dashboard locally with auto-reload
	poetry run uvicorn pricing_service.main:app --host $(HOST) --port $(PORT) --reload

stop: ## Kill whatever is listening on PORT (e.g. a backgrounded `make run`)
	lsof -ti:$(PORT) | xargs -r kill

# --------------------------------------------------------------------------
# Docker
# --------------------------------------------------------------------------
build: ## Build the Docker image
	docker build -t $(REPO_NAME) -f Dockerfile .

run-container: ## Run the built image and publish the API port
	docker run --rm -p $(PORT):8000 $(REPO_NAME)
