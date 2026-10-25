help:  ## Show help
	@grep -E '^[.0-9a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

setup: ## Setup development environment
	uv venv --clear --system-site-packages
	uv sync --locked --all-extras
	uv run pre-commit install

format: ## Run pre-commit hooks
	uv run pre-commit run -a

test: ## Run tests except e2e (hardware is checked by docs/acceptance.md)
	uv run pytest -v -m "not e2e"

test-e2e: ## Run WebUI full-stack E2E (live uvicorn + HTTP/WS/MJPEG)
	uv run pytest -v -m e2e --timeout=180

playwright-install: ## Install Playwright-managed Chromium when system chromium is unavailable
	uv run playwright install chromium

type: ## Run type check
	uv run pyright

api: ## Run backend WebAPI server
	uv run python -m web.api

ui: ## Run UI frontend server
	uv run python -m web.ui

# Klipper もカメラも fake にした backend（8099）と frontend（8098）を一時ディレクトリで起動する。
# mDNS と自己更新は切る（実運用の機体一覧やソース更新へ干渉させない）
dev: ## Run fake backend and frontend together (isolated ports and temp dir)
	uv run python -m web.dev

check: format type test ## Run format, type check and tests
