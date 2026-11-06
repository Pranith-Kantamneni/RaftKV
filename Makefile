SHELL := /bin/bash
PYTHON ?= .venv/bin/python
VENV ?= .venv
DATA_DIR ?= data
DURATION ?= 10
RATE ?= 6
SCENARIO ?= normal

.DEFAULT_GOAL := help

.PHONY: help install run dev dev-backend dev-frontend test test-backend test-frontend \
	typecheck build benchmark experiment smoke docker-build docker-up docker-down \
	docker-logs docker-ps clean clean-data

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create the venv and install Python + frontend dependencies
	python3 -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt
	cd frontend && npm ci

run: ## Run the controller and cluster locally on :8000
	DATA_DIR=$(DATA_DIR) $(PYTHON) -m backend.main

dev-backend: ## Run the API with auto-reload-friendly logging
	DATA_DIR=$(DATA_DIR) $(PYTHON) -m backend.main

dev-frontend: ## Run the Vite dev server on :5173 (proxies :8000)
	cd frontend && npm run dev

dev: ## Run backend + Vite together
	@sh -c 'DATA_DIR=$(DATA_DIR) $(PYTHON) -m backend.main & backend=$$!; trap "kill $$backend 2>/dev/null || true" EXIT INT TERM; cd frontend && npm run dev'

test: test-backend test-frontend ## Run every backend and frontend test

test-backend: ## Run Python unit + live-process integration tests
	$(PYTHON) -m pytest -q

test-frontend: ## Run Vitest component/client tests
	cd frontend && npm test

typecheck: ## TypeScript strict type check
	cd frontend && npm run typecheck

build: ## Production frontend build
	cd frontend && npm run build

benchmark: ## Run the real six-scenario benchmark suite
	$(PYTHON) scripts/benchmark.py --duration $(DURATION) --rate $(RATE)

experiment: ## Run one scenario (SCENARIO=normal|leader_crash|partition|...)
	$(PYTHON) scripts/benchmark.py --scenario $(SCENARIO) --duration $(DURATION) --rate $(RATE)

smoke: ## Verify a running cluster with a committed write/read
	$(PYTHON) scripts/smoke_test.py

docker-build: ## Build controller and dashboard images
	docker compose build

docker-up: ## Start controller, dashboard and Prometheus
	docker compose up -d --build

docker-down: ## Stop and remove the compose stack (keeps the data volume)
	docker compose down

docker-logs: ## Follow controller logs
	docker compose logs -f controller

docker-ps: ## Show compose service status
	docker compose ps

clean: ## Remove build/test caches (does not touch data)
	rm -rf frontend/dist frontend/tsconfig.tsbuildinfo .pytest_cache
	find backend frontend/src -type d -name __pycache__ -prune -exec rm -rf {} +

clean-data: ## Delete local cluster data and experiment reports (destructive)
	@read -p "Delete $(DATA_DIR)? [y/N] " answer; [[ "$$answer" == "y" || "$$answer" == "Y" ]] && rm -rf -- "$(DATA_DIR)" || true
