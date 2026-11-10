# Postgres 16 from Homebrew is keg-only; put it on PATH if present.
export PATH := /opt/homebrew/opt/postgresql@16/bin:/usr/local/opt/postgresql@16/bin:$(PATH)

DB_NAME ?= setu

.PHONY: dev test lint typecheck db-create db-migrate bootstrap demo api-types api-types-check web-build

dev:
	./scripts/dev.sh

# Server + SDK + demo tests (including the browser tests when Playwright's Chromium is installed:
# `uv run playwright install chromium`; SKIP_E2E=1 skips them), then the web app's unit tests.
test:
	cd server && uv run pytest
	cd web && npm test

lint:
	cd server && uv run ruff check . ../sdk/python ../examples && uv run ruff format --check . ../sdk/python ../examples

typecheck: api-types-check
	cd server && uv run mypy app
	cd server && PYTHONPATH=../examples uv run mypy --strict ../sdk/python/setu_sdk ../examples/cross_department
	cd web && npm run typecheck

# The web app's TypeScript types come from the server's OpenAPI document (spec section 16).
# Regenerate after changing anything in server/app/schemas or the API routes.
OPENAPI = import json; from app.main import app; print(json.dumps(app.openapi(), indent=1, sort_keys=True))
api-types:
	cd server && uv run python -c "$(OPENAPI)" > ../web/openapi.json
	cd web && npm run gen:api

# Fails if web/src/api/schema.d.ts is out of date with the server.
api-types-check:
	@cd server && uv run python -c "$(OPENAPI)" > ../web/openapi.check.json
	@cd web && npx openapi-typescript openapi.check.json -o src/api/schema.check.d.ts >/dev/null
	@cd web && (cmp -s openapi.json openapi.check.json && cmp -s src/api/schema.d.ts src/api/schema.check.d.ts) \
		&& rm -f openapi.check.json src/api/schema.check.d.ts \
		|| (rm -f openapi.check.json src/api/schema.check.d.ts; echo "web API types are stale: run make api-types"; exit 1)

web-build:
	cd web && npm run build

db-create:
	@psql -d postgres -Atc "select 1 from pg_database where datname='$(DB_NAME)'" | grep -q 1 \
		&& echo "database $(DB_NAME) already exists" \
		|| createdb $(DB_NAME)

db-migrate:
	cd server && uv run alembic upgrade head

# First org + department + org_admin. Password comes from ADMIN_PASSWORD or a prompt.
#   make bootstrap ORG=Acme DEPT="Operations" SLUG=ops EMAIL=admin@acme.test NAME="Ada Admin"
bootstrap:
	cd server && uv run python -m app.cli bootstrap --org "$(ORG)" --dept-name "$(DEPT)" \
		--dept-slug "$(SLUG)" --email "$(EMAIL)" --name "$(NAME)"

# The section 2 story, end to end, against a running Setu (make dev in another terminal).
# Needs an org admin: make bootstrap ... , then DEMO_ADMIN_EMAIL / DEMO_ADMIN_PASSWORD.
# `make demo ARGS=--interactive` sets the same world up and lets you drive it from the web app.
demo:
	cd server && PYTHONPATH=../examples uv run python -m cross_department.run_demo $(ARGS)
