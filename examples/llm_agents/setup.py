"""Wire the three LLM agents into Setu through the public API; prints how to log in.

    python -m llm_agents.setup  (needs SETU_URL, DEMO_ADMIN_EMAIL, DEMO_ADMIN_PASSWORD)

Everyone sits in the admin's department, so no allow_talk policy is needed. The admin grants
the researcher a delegation limited to the reviewer's skill, which is what lets it ask the
reviewer on the admin's behalf. Re-runnable: existing agents are reused.
"""

import json
import os
import secrets
from typing import Any

import httpx
from agent_server import ROLES, STATE_FILE
from cross_department.seed import Api


def main() -> None:
    url = os.environ.get("SETU_URL", "http://127.0.0.1:8000")
    api = Api(url)
    api.login(os.environ["DEMO_ADMIN_EMAIL"], os.environ["DEMO_ADMIN_PASSWORD"])
    dept = api.me["department_id"]
    tag = secrets.token_hex(2)

    agents: dict[str, dict[str, Any]] = {}
    for role, cfg in ROLES.items():
        card = f"http://127.0.0.1:{cfg['port']}/.well-known/agent-card.json"
        r = api.http.post(
            "/v1/agents/register",
            json={"card_url": card, "department_id": dept, "handle": f"{role}-{tag}"},
            headers=api.headers,
        )
        if r.status_code == 409:
            found = [a for a in api.call("GET", "/v1/agents") if a.get("card_url") == card]
            if not found:
                raise RuntimeError(f"{role}: {r.text}")
            agents[role] = found[0]
        elif r.status_code == 201:
            agents[role] = r.json()
        else:
            raise RuntimeError(f"register {role} -> {r.status_code}: {r.text}")

    key = api.call(
        "POST", f"/v1/participants/{agents['researcher']['participant_id']}/api-keys", {}
    )["key"]
    deleg = api.call(
        "POST",
        "/v1/delegations",
        {
            "to_participant_id": agents["researcher"]["participant_id"],
            "scopes": ["skill:review.critique"],
        },
    )
    room = api.call("POST", "/v1/rooms", {"name": f"llm-agents-{tag}", "visibility": "department"})
    for a in agents.values():
        api.call("POST", f"/v1/rooms/{room['id']}/members", {"participant_id": a["participant_id"]})

    STATE_FILE.write_text(
        json.dumps(
            {
                "setu_url": url,
                "api_key": key,
                "delegation_id": deleg["id"],
                "reviewer_handle": agents["reviewer"]["handle"],
                "room_id": room["id"],
            }
        )
    )
    STATE_FILE.chmod(0o600)
    print(f"room: {room['name']}  ({url.replace('8000', '5174')}/)")
    print("login:", os.environ["DEMO_ADMIN_EMAIL"])
    for role, a in agents.items():
        print(f"  @{a['handle']}  ({role}, port {ROLES[role]['port']})")


if __name__ == "__main__":
    _ = httpx  # imported so a missing dependency fails early
    main()
