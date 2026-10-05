.PHONY: install dev test lint docker up observability clean

install:            ## Create venv and install with dev extras
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev,ai]"

dev:                ## Run with auto-reload on http://localhost:8000
	.venv/bin/uvicorn atlas.api:app --reload

test:               ## Run the test suite with coverage
	.venv/bin/pytest --cov=atlas --cov-report=term-missing

lint:
	.venv/bin/ruff check .

docker:
	docker build -t atlas-one:latest .

up:                 ## Run in Docker
	docker compose up --build

observability:      ## Run with Prometheus + Grafana
	docker compose --profile observability up --build

clean:
	rm -rf .pytest_cache .ruff_cache .coverage **/__pycache__
