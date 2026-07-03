.PHONY: up down logs test lint format

# Create .env from the template on first run so the stack works from a clean checkout.
.env:
	cp .env.example .env

up: .env ## Build and start the full stack
	docker compose up --build

down: ## Stop the stack and remove containers
	docker compose down

logs: ## Follow logs from all services
	docker compose logs -f

test: .env ## Run the test suite (unit + functional, no services needed)
	docker compose run --rm --no-deps api pytest

lint: ## Lint the codebase
	docker compose run --rm --no-deps api ruff check .

format: ## Auto-format the codebase
	docker compose run --rm --no-deps api ruff format .
