# Setu: working notes for Claude

**BUILD_SPEC.md is the source of truth.** (Overview and setup: README.md. Every phase is built; later changes and deviations are logged in `docs/decisions.md`: add a numbered entry for any new decision.) Read it (especially section 8, A2A) before changing anything. If this file and the spec disagree, the spec wins.

## Workflow
- Work in phases (spec section 15). Do one phase, run checks, stop for review. Proceed only when told "Proceed to Phase N."
- Do not add Docker, Compose or Redis (spec section 5). Realtime fan-out is Postgres LISTEN/NOTIFY with ids only.
- Do not build anything under Non-goals (spec section 4). Do not silently swap the stack (section 5); propose an alternative and wait.
- Routing is deterministic: no LLM in `server/app/core/routing.py` or the message path.

## Conventions (spec section 16)
- Type everything. `mypy --strict` on `server/app/core` and `server/app/a2a`.
- Pydantic models in `server/app/schemas/` are the contract; generate TS types for web from OpenAPI.
- Integration tests run against a real local Postgres (pytest fixtures create and drop a throwaway database per session). No Docker, no testcontainers, no mocks for the database. Include contract tests against a reference A2A agent built with `a2a-sdk`.
- Migrations are forward-only; never edit an applied one.
- Secrets only via env; `.env.example` is committed, `.env` is not. Agent credentials are encrypted at rest.
- Small commits with conventional messages.
- Ambiguous requirement: choose the simplest option, record it in `docs/decisions.md`, continue. Ask only if the choice is expensive to reverse.

## Commands
- `make db-create db-migrate dev`: create the DB, migrate, start api (8000) + web (5173). No Docker, no Redis.
- `make test` / `make lint` / `make typecheck`: run in `server/` via `uv`; `make test` uses a throwaway database
- Health check: `GET /healthz` on port 8000
