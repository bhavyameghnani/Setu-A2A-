"""One investment-banking agent per process, each on its own port (9301-9305).

    ROLE=valuation-analyst python -m ib_server

The model only writes text. Agents that consult a colleague do it through Setu's A2A facade, so
policy, delegation and tracing apply; the credentials for that are written by `ib_seed.py`.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from ib_roles import ROLES, Role
from llm import chat
from setu_sdk.a2a_serve import A2ARequest, Skill, build_a2a_app, run

STATE_FILE = Path(os.environ.get("IB_AGENTS_STATE", Path(__file__).parent / "state.json"))


def load_state() -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(STATE_FILE.read_text())
        return data
    except (OSError, ValueError):
        return {}


async def ask_peer(req: A2ARequest, me: Role, peer: Role, question: str, draft: str) -> str:
    """A plain A2A 1.0 SendMessage to Setu's facade for the colleague (see risk_agent.py)."""
    state = load_state()
    mine = state.get("agents", {}).get(me.key, {})
    if not mine.get("api_key") or not mine.get("delegation_id"):
        return "(colleague not wired up: run ib_seed.py)"
    headers = {"X-API-Key": mine["api_key"], "A2A-Version": "1.0"}
    if tp := req.headers.get("traceparent"):
        headers["traceparent"] = tp  # continue the trace Setu started
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "contextId": req.setu.get("room_id") or req.context_id,
                "parts": [{"text": f"Request: {question}\n\nDraft from {me.name}:\n{draft}"}],
                "metadata": {"setu": {"skill": peer.skill, "delegation_id": mine["delegation_id"]}},
            }
        },
    }
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(f"{state['setu_url']}/a2a/{peer.key}", json=body, headers=headers)
    resp.raise_for_status()
    reply = resp.json()
    if "error" in reply:
        raise RuntimeError(f"Setu refused the call to {peer.name}: {reply['error'].get('message')}")
    task = reply["result"]["task"]
    if task["status"]["state"] != "TASK_STATE_COMPLETED":
        raise RuntimeError(f"{peer.name} did not complete ({task['status']['state']})")
    return str(task["artifacts"][0]["parts"][0].get("text", ""))


def build(key: str) -> tuple[Any, int]:
    role = ROLES[key]
    port = int(os.environ.get("PORT", role.port))

    async def handle(req: A2ARequest) -> str:
        await req.progress(f"{role.name} is working")
        answer = await chat(role.system, req.text)
        if role.peer is None:
            return answer
        peer = ROLES[role.peer]
        await req.progress(f"consulting {peer.name}")
        advice = await ask_peer(req, role, peer, req.text, answer)
        return f"{role.name.upper()}:\n{answer}\n\n{peer.name.upper()} ADDS:\n{advice}"

    app = build_a2a_app(
        handle,
        name=role.name,
        description=f"{role.name}: {role.skill_name} ({role.dept}). Fictional demo agent.",
        url=os.environ.get("PUBLIC_URL", f"http://127.0.0.1:{port}"),
        skills=[Skill(role.skill, role.skill_name, role.skill_name, [role.dept])],
    )
    return app, port


if __name__ == "__main__":
    app, port = build(os.environ["ROLE"])
    run(app, port=port)
