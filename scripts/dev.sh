#!/usr/bin/env bash
# Starts the API (port 8000) and web (port 5173). Checks Postgres is reachable first.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then set -a; . ./.env; set +a; fi
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://localhost:5432/setu}"

(cd server && uv run python - <<'PY'
import asyncio, os, sys
import asyncpg
from sqlalchemy.engine import make_url

u = make_url(os.environ["DATABASE_URL"])

async def main() -> None:
    conn = await asyncpg.connect(user=u.username, password=u.password, host=u.host,
                                 port=u.port, database=u.database, timeout=5)
    await conn.close()

try:
    asyncio.run(main())
except Exception as exc:
    sys.exit(f"Postgres not reachable at {u.render_as_string(hide_password=True)}: {exc}\n"
             "Start it (brew services start postgresql@16) and run: make db-create db-migrate")
PY
)

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd server && exec uv run uvicorn app.main:app --reload --port 8000) &
pids+=($!)

if [ -d web ] && [ -f web/package.json ]; then
  [ -d web/node_modules ] || (cd web && npm install)
  (cd web && exec npm run dev) &
  pids+=($!)
fi

wait
