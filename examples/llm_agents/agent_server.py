"""One LLM-backed A2A agent per process, each on its own port.

    ROLE=researcher PORT=9201 python -m llm_agents.agent_server

Roles: researcher (9201) answers, then asks the reviewer through Setu's A2A facade; reviewer
(9202) critiques; summarizer (9203) summarizes. The model only writes text. Who may talk to whom
is decided by Setu (policy, delegation), never by the model.

The researcher needs Setu credentials that exist only after registration, so `setup.py` writes
them to a state file that is re-read on every call.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from llm import chat
from setu_sdk.a2a_serve import A2ARequest, Skill, build_a2a_app, run

STATE_FILE = Path(os.environ.get("LLM_AGENTS_STATE", Path(__file__).parent / "state.json"))

ROLES: dict[str, dict[str, Any]] = {
    "researcher": {
        "port": 9201,
        "name": "Researcher-Agent",
        "skill": Skill("research.answer", "Research answer", "Answers a question", ["research"]),
        "system": "You are a concise research assistant. Answer in at most 5 sentences.",
    },
    "reviewer": {
        "port": 9202,
        "name": "Reviewer-Agent",
        "skill": Skill("review.critique", "Critique", "Fact-checks and critiques text", ["review"]),
        "system": (
            "You are a strict reviewer. Point out errors, gaps or doubtful claims in the text. "
            "Reply with a verdict (OK / NEEDS WORK) and at most 3 short bullets."
        ),
    },
    "summarizer": {
        "port": 9203,
        "name": "Summarizer-Agent",
        "skill": Skill("summary.write", "Summarize", "Summarizes text", ["summary"]),
        "system": "You summarize the given text in two sentences.",
    },
}


def load_state() -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(STATE_FILE.read_text())
        return data
    except (OSError, ValueError):
        return {}


async def ask_reviewer(req: A2ARequest, question: str, answer: str) -> str:
    """A plain A2A 1.0 SendMessage to Setu's facade for the reviewer (see risk_agent.py)."""
    st = load_state()
    if not st.get("api_key"):
        return "(reviewer not wired up: run setup.py)"
    headers = {"X-API-Key": st["api_key"], "A2A-Version": "1.0"}
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
                "parts": [{"text": f"Question: {question}\n\nAnswer to review:\n{answer}"}],
                "metadata": {
                    "setu": {"skill": "review.critique", "delegation_id": st["delegation_id"]}
                },
            }
        },
    }
    async with httpx.AsyncClient(timeout=90) as http:
        resp = await http.post(
            f"{st['setu_url']}/a2a/{st['reviewer_handle']}", json=body, headers=headers
        )
    resp.raise_for_status()
    reply = resp.json()
    if "error" in reply:
        raise RuntimeError(f"Setu refused the review call: {reply['error'].get('message')}")
    task = reply["result"]["task"]
    if task["status"]["state"] != "TASK_STATE_COMPLETED":
        raise RuntimeError(f"review did not complete ({task['status']['state']})")
    return str(task["artifacts"][0]["parts"][0].get("text", ""))


def build(role: str) -> Any:
    cfg = ROLES[role]
    port = int(os.environ.get("PORT", cfg["port"]))

    async def handle(req: A2ARequest) -> str:
        await req.progress(f"{cfg['name']} is thinking")
        out = await chat(cfg["system"], req.text)
        if role != "researcher":
            return out
        await req.progress("asking the Reviewer to check my answer")
        review = await ask_reviewer(req, req.text, out)
        return f"ANSWER:\n{out}\n\nREVIEWER SAYS:\n{review}"

    return build_a2a_app(
        handle,
        name=cfg["name"],
        description=f"{cfg['name']} (LLM-backed, served over A2A)",
        url=os.environ.get("PUBLIC_URL", f"http://127.0.0.1:{port}"),
        skills=[cfg["skill"]],
    ), port


if __name__ == "__main__":
    app, port = build(os.environ["ROLE"])
    run(app, port=port)
