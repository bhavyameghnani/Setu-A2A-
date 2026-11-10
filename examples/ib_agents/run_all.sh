#!/usr/bin/env bash
# Start the five investment-banking agents (ports 9301-9305) in the background, then seed Setu.
# Re-run it any time the agents have stopped: registrations live in Postgres.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
mkdir -p examples/ib_agents/.logs
for role in valuation-analyst credit-risk contract-reviewer kyc-screener conflicts-checker; do
  port=$(cd server && PYTHONPATH=../examples/ib_agents .venv/bin/python -c "from ib_roles import ROLES; print(ROLES['$role'].port)")
  if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then continue; fi
  (cd server && PYTHONPATH=../examples/ib_agents:../examples/llm_agents:../examples ROLE=$role \
     nohup .venv/bin/python -m ib_server >../examples/ib_agents/.logs/$role.log 2>&1 &)
done
for port in 9301 9302 9303 9304 9305; do
  for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:$port/.well-known/agent-card.json" >/dev/null && break
    sleep 0.5
  done
done
if [ "${1:-}" != "--no-seed" ]; then
  (cd server && PYTHONPATH=../examples/ib_agents:../examples/llm_agents:../examples \
     SETU_URL="${SETU_URL:-http://127.0.0.1:8000}" \
     DEMO_ADMIN_EMAIL="${DEMO_ADMIN_EMAIL:-admin@llm.test}" \
     DEMO_ADMIN_PASSWORD="${DEMO_ADMIN_PASSWORD:-setu-admin-123}" \
     .venv/bin/python -m ib_seed)
fi
