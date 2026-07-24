.PHONY: help install install-prod test lint format typecheck clean \
        frontend-install frontend-dev frontend-build frontend-lint \
        run-api run-cli run-dev build

VENV    := .venv
PY      := $(VENV)/bin/python
UV      := uv
NPM     := cd frontend && npm

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Backend ──────────────────────────────────────────────────────────

install: ## Install backend (dev + web extras) into .venv
	$(UV) venv $(VENV) --python 3.12
	$(VENV)/bin/pip install -e ".[dev,web]"

install-global: ## Install as global CLI tool (uv tool install)
	$(UV) tool install -e ".[web,dev]" --force

install-prod: ## Install production deps only (web extra)
	$(UV) venv $(VENV) --python 3.12
	$(VENV)/bin/pip install -e ".[web]"

test: ## Run pytest
	$(VENV)/bin/python -m pytest $(ARGS)

lint: ## Lint with ruff
	$(VENV)/bin/ruff check .

format: ## Format with ruff
	$(VENV)/bin/ruff format .

typecheck: ## Type check with mypy
	$(VENV)/bin/mypy openkb

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache *.egg-info build dist

# ── Frontend ────────────────────────────────────────────────────────

frontend-install: ## Install frontend dependencies
	$(NPM) install

frontend-dev: ## Start Vite dev server (proxies /api to openkb-web)
	$(NPM) run dev

frontend-build: ## Build frontend bundle into openkb/web/
	$(NPM) run build

frontend-lint: ## Lint frontend with ESLint
	$(NPM) run lint

# ── Run ──────────────────────────────────────────────────────────────

run-api: ## Start REST API + Workbench server (port 7566)
	$(VENV)/bin/python -m openkb.api

run-cli: ## Start OpenKB CLI
	$(VENV)/bin/openkb

run-dev: run-api frontend-dev ## Start API server + Vite dev server together

# ── All-in-one ───────────────────────────────────────────────────────

build: frontend-build ## Build everything (frontend bundle for packaging)

dev: install frontend-install ## Full dev setup: install backend + frontend deps

all: dev build ## Install everything and build frontend bundle
