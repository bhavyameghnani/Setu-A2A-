#!/usr/bin/env bash
# Start the three LLM agents (9201/9202/9203) in the background; logs in .logs dir.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
mkdir -p examples/llm_agents/.logs
for role in researcher reviewer summarizer; do
  (cd server && PYTHONPATH=../examples/llm_agents:../examples ROLE=$role \
     nohup .venv/bin/python -m agent_server >../examples/llm_agents/.logs/$role.log 2>&1 &)
done
