# ==================== Variables ====================
REGISTRY ?= ghcr.io/jeanclode-hq
VERSION  ?= 1.3.2 # x-release-please-version

# ==================== Quality ====================
qa: ## Run pre-commit checks (linting, formatting)
	uv tool run pre-commit run --all-files

# ==================== Tests ====================
backend-test: ## Run backend tests
	@if [ ! -f .env ]; then echo "Error: .env file not found. Copy .env.example to .env"; exit 1; fi
	@set -a && . ./.env && set +a && cd backend && \
		DATABASE_URL="postgresql://$${POSTGRES_USER}:$${POSTGRES_PASSWORD}@localhost:5432/$${POSTGRES_DB}" \
		uv run pytest -v -n auto

cli-test: ## Run CLI tests
	cd cli && uv run pytest -v

eval: ## Run LLM eval tests (requires GOOGLE_API_KEY / GEMINI_API_KEY)
	cd cli && uv run pytest tests/eval/ -v -m eval

test: backend-test cli-test ## Run all tests

frontend-test: ## Run frontend tests
	cd frontend && pnpm test

# ==================== Docker Build ====================
# Default to linux/amd64 so images built on Apple Silicon still run on the
# (amd64) dev/prod clusters — override with PLATFORM=... if needed.
PLATFORM ?= linux/amd64

backend-build: ## Build the backend Docker image
	docker build $(if $(PLATFORM),--platform $(PLATFORM),) -t $(REGISTRY)/backend:$(VERSION) ./backend

cli-build: ## Build the CLI worker Docker image
	docker build $(if $(PLATFORM),--platform $(PLATFORM),) -t $(REGISTRY)/cli:$(VERSION) cli/

frontend-build: ## Build the frontend Docker image
	docker build $(if $(PLATFORM),--platform $(PLATFORM),) -t $(REGISTRY)/frontend:$(VERSION) -f frontend/Dockerfile .

security-proxy-build: ## Build the security proxy Docker image
	docker build $(if $(PLATFORM),--platform $(PLATFORM),) -t $(REGISTRY)/security-proxy:$(VERSION) ./security-proxy

build: backend-build frontend-build cli-build security-proxy-build ## Build all Docker images

# ==================== Docker Push ====================
backend-push: ## Push the backend Docker image (versioned + latest)
	docker tag $(REGISTRY)/backend:$(VERSION) $(REGISTRY)/backend:latest
	docker push $(REGISTRY)/backend:$(VERSION)
	docker push $(REGISTRY)/backend:latest

frontend-push: ## Push the frontend Docker image (versioned + latest)
	docker tag $(REGISTRY)/frontend:$(VERSION) $(REGISTRY)/frontend:latest
	docker push $(REGISTRY)/frontend:$(VERSION)
	docker push $(REGISTRY)/frontend:latest

cli-push: ## Push the CLI worker Docker image (versioned + latest)
	docker tag $(REGISTRY)/cli:$(VERSION) $(REGISTRY)/cli:latest
	docker push $(REGISTRY)/cli:$(VERSION)
	docker push $(REGISTRY)/cli:latest

security-proxy-push: ## Push the security proxy Docker image (versioned + latest)
	docker tag $(REGISTRY)/security-proxy:$(VERSION) $(REGISTRY)/security-proxy:latest
	docker push $(REGISTRY)/security-proxy:$(VERSION)
	docker push $(REGISTRY)/security-proxy:latest

push: backend-push frontend-push cli-push security-proxy-push ## Push all Docker images (versioned + latest)

# ==================== Code Generation ====================
generate-types: ## Generate typed API client from backend OpenAPI spec
	@cd backend && uv run python generate_openapi.py
	@pnpm --filter @jeanclode/api-types exec openapi-ts --input $(CURDIR)/packages/api-types/openapi.json --output $(CURDIR)/packages/api-types --client @hey-api/client-fetch
	@echo "export { client } from './client.gen';" >> packages/api-types/index.ts
	@printf '{\n  "name": "@jeanclode/api-types",\n  "version": "0.0.0",\n  "private": true,\n  "type": "module",\n  "main": "./index.ts",\n  "types": "./index.ts",\n  "exports": {\n    ".": "./index.ts",\n    "./client": "./client.gen.ts"\n  },\n  "devDependencies": {\n    "@hey-api/client-fetch": "^0.8.4",\n    "@hey-api/openapi-ts": "^0.73.0"\n  }\n}\n' > packages/api-types/package.json
	@rm -f packages/api-types/openapi.json
	@for f in packages/api-types/*.ts; do [ -n "$$(tail -c1 "$$f")" ] && echo >> "$$f"; done
	@echo "✅ SDK generated in packages/api-types/"

# ==================== Development ====================
backend-run: ## Run backend API server locally (requires make compose-test)
	@if [ ! -f .env ]; then echo "Error: .env file not found. Copy .env.example to .env"; exit 1; fi
	@set -a && . ./.env && set +a && cd backend && \
		DATABASE_URL="postgresql://$${POSTGRES_USER}:$${POSTGRES_PASSWORD}@localhost:5432/$${POSTGRES_DB}" \
		REDIS_URL="redis://localhost:6379/0" \
		DOCKER_HOST="tcp://localhost:2375" \
		JEANCLODE_CONFIG="configs/kubernetes.yaml" \
		uv run watchfiles "python run.py" api configs

frontend-run: ## Run frontend dev server locally
	@if [ ! -f .env ]; then echo "Error: .env file not found. Copy .env.example to .env"; exit 1; fi
	@set -a && . ./.env && set +a && cd frontend && pnpm dev

# ==================== Website ====================
CLOUDFLARE_PROJECT ?= jeanclode-website

website-run: ## Run website dev server (http://localhost:3000)
	cd website && pnpm dev

website-generate: ## Generate static website into website/.output/public
	cd website && pnpm generate

website-preview: ## Preview the generated static site locally
	cd website && pnpm preview

website-deploy: website-generate ## Build and deploy website to Cloudflare Pages (requires wrangler login)
	npx wrangler --cwd website/dist pages deploy --project-name $(CLOUDFLARE_PROJECT)

# ==================== Docker Compose ====================
compose-all: ## Start all services (Postgres, Redis, backend)
	docker compose --profile all down -v --remove-orphans && \
	docker compose --profile all up --build

compose-app: ## Start app only (bring your own Postgres/Redis)
	docker compose --profile app down -v --remove-orphans && \
	docker compose --profile app up --build

compose-test: ## Start database only (for local dev/testing)
	docker compose --profile test down -v --remove-orphans && \
	docker compose --profile test up --build

compose-down: ## Stop all services and remove volumes
	docker compose --profile all down -v --remove-orphans

# ==================== Database ====================
migrate: ## Run database migrations
	@if [ ! -f .env ]; then echo "Error: .env file not found. Copy .env.example to .env"; exit 1; fi
	@set -a && . ./.env && set +a && cd backend && \
		DATABASE_URL="postgresql://$${POSTGRES_USER}:$${POSTGRES_PASSWORD}@localhost:5432/$${POSTGRES_DB}" \
		uv run alembic upgrade head

# ==================== Help ====================
help: ## Show this help
	@echo "Jeanclode - Available Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
