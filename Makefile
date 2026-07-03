.PHONY: up down logs test lint format migrate makemigration

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

migrate: .env ## Apply all pending migrations (upgrade head)
	docker compose run --rm api alembic upgrade head

makemigration: .env ## Autogenerate a migration: make makemigration m="message"
	docker compose run --rm api alembic revision --autogenerate -m "$(m)"
