.PHONY: help dev down logs check fmt lint types test test-unit migrate revision shell db-reset build

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

dev: ## Start the full stack
	docker compose up -d --build

down: ## Stop the stack (volumes preserved)
	docker compose down

logs: ## Follow application logs
	docker compose logs -f api

check: lint types test ## Everything CI runs
	@echo "All gates passed."

lint: ## Lint and format check
	cd backend && uv run ruff check . && uv run ruff format --check .

fmt: ## Auto-format
	cd backend && uv run ruff check --fix . && uv run ruff format .

types: ## Type check
	cd backend && uv run mypy

test: ## Full test suite
	cd backend && uv run pytest -v

test-unit: ## Fast loop, no database
	cd backend && uv run pytest -m "not integration"

migrate: ## Apply migrations
	cd backend && uv run alembic upgrade head

revision: ## Create a migration: make revision m="add repos"
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

db-reset: ## DESTROY the database and rebuild it
	docker compose down -v && docker compose up -d postgres && sleep 5 && $(MAKE) migrate

build: ## Build the image
	docker compose build api

check: lint types migration-check test ## Everything CI runs
	@echo "All gates passed."

migration-check: ## Fail if models and migrations have drifted
	cd backend && uv run alembic check
