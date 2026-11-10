"""Set up the investment-banking demo in Setu through the public API (re-runnable).

    python -m ib_seed       (needs SETU_URL, DEMO_ADMIN_EMAIL, DEMO_ADMIN_PASSWORD; the five agent
                             servers must already be running so their cards can be fetched)

Creates the Finance, Legal and Compliance departments, two people in each, registers the five
agents, mints the keys and delegations the two agents that consult a colleague need, adds the
cross-department policies (including a four-eyes approval for KYC screening), a deal room with
everyone in it, and the teams. Everything it creates is fictional.
"""

import json
import os
from typing import Any

from cross_department.seed import Api
from ib_roles import DEPARTMENTS, PASSWORD, PEOPLE, ROLES, ROOM, ROOM_TOPIC
from ib_server import STATE_FILE

OK = (200, 201)


def call(api: Api, method: str, path: str, body: Any = None, *, ok: tuple[int, ...] = OK) -> Any:
    return api.call(method, path, body, ok=ok)


def main() -> None:
    url = os.environ.get("SETU_URL", "http://127.0.0.1:8000")
    admin = Api(url)
    admin.login(os.environ["DEMO_ADMIN_EMAIL"], os.environ["DEMO_ADMIN_PASSWORD"])

    # --- departments (reuse any that already exist)
    have = {d["slug"]: d["id"] for d in call(admin, "GET", "/v1/departments")}
    depts: dict[str, str] = {}
    for slug, name in DEPARTMENTS:
        depts[slug] = (
            have.get(slug)
            or call(admin, "POST", "/v1/departments", {"name": name, "slug": slug})["id"]
        )

    # --- people: an admin and a member per department
    emails: dict[str, str] = {}
    for slug, (adm, mem) in PEOPLE.items():
        for (handle, display), roles in ((adm, ["dept_admin"]), (mem, ["dept_member"])):
            emails[handle] = f"{handle}@bank.test"
            call(
                admin,
                "POST",
                "/v1/humans",
                {
                    "department_id": depts[slug],
                    "display_name": display,
                    "handle": handle,
                    "email": emails[handle],
                    "password": PASSWORD,
                    "roles": roles,
                },
                ok=(200, 201, 409),  # 409: already there from an earlier run
            )
    admins = {slug: Api(url) for slug in PEOPLE}
    for slug, (adm, _) in PEOPLE.items():
        admins[slug].login(emails[adm[0]], PASSWORD)

    # --- agents: register by card URL (an earlier registration of the same card is reused)
    registered = {a.get("card_url"): a for a in call(admin, "GET", "/v1/agents")}
    agents: dict[str, dict[str, Any]] = {}
    for key, role in ROLES.items():
        card = f"http://127.0.0.1:{role.port}/.well-known/agent-card.json"
        if card in registered:
            agents[key] = registered[card]
            continue
        agents[key] = call(
            admin,
            "POST",
            "/v1/agents/register",
            {"card_url": card, "department_id": depts[role.dept], "handle": key},
        )

    # --- keys and delegations for the agents that consult a colleague. A delegation is a human
    #     lending authority for named skills, so the owning department's admin grants it.
    state: dict[str, Any] = {"setu_url": url, "agents": {}}
    for key, role in ROLES.items():
        if role.peer is None:
            continue
        pid = agents[key]["participant_id"]
        api_key = call(admin, "POST", f"/v1/participants/{pid}/api-keys", {})["key"]
        grant = call(
            admins[role.dept],
            "POST",
            "/v1/delegations",
            {"to_participant_id": pid, "scopes": [f"skill:{ROLES[role.peer].skill}"]},
        )
        state["agents"][key] = {"api_key": api_key, "delegation_id": grant["id"], "peer": role.peer}
    STATE_FILE.write_text(json.dumps(state))
    STATE_FILE.chmod(0o600)

    # --- policies: cross-department traffic is denied until a rule allows it
    rules: list[tuple[str, dict[str, Any]]] = []
    slugs = [s for s, _ in DEPARTMENTS]
    names = dict(DEPARTMENTS)
    for a in slugs:
        for b in slugs:
            if a != b:
                rules.append(
                    (
                        f"{names[a]} may ask {names[b]}",
                        {
                            "type": "allow_talk",
                            "from_department": depts[a],
                            "to_department": depts[b],
                        },
                    )
                )
    rules.append(
        (
            "KYC screening needs a Compliance sign-off (four eyes)",
            {
                "type": "require_approval",
                "skill": ROLES["kyc-screener"].skill,
                "approver_department": depts["compliance"],
                "approver_role": "dept_admin",
            },
        )
    )
    existing = {p["name"] for p in call(admin, "GET", "/v1/policies")}
    for name, rule in rules:
        if name not in existing:
            call(admin, "POST", "/v1/policies", {"name": name, "rule": rule})

    # --- the deal room, with every agent and person in it
    finance_admin = admins["finance"]
    rooms = {r["name"]: r for r in call(finance_admin, "GET", "/v1/rooms")}
    room = rooms.get(ROOM) or call(
        finance_admin,
        "POST",
        "/v1/rooms",
        {
            "name": ROOM,
            "topic": ROOM_TOPIC,
            "visibility": "cross_department",
            "owning_department_id": depts["finance"],
        },
    )
    people_ids = {h: finance_admin.person(emails[h], PASSWORD)["id"] for h in emails}
    for pid in [a["participant_id"] for a in agents.values()] + list(people_ids.values()):
        r = finance_admin.http.post(
            f"/v1/rooms/{room['id']}/members",
            json={"participant_id": pid, "role": "member"},
            headers=finance_admin.headers,
        )
        if r.status_code not in (200, 201, 409):
            raise RuntimeError(f"add member -> {r.status_code}: {r.text}")

    # --- teams: one per department, plus a core team across them
    teams = {t["name"] for t in call(admin, "GET", "/v1/teams")}
    by_dept: dict[str, list[str]] = {s: [] for s in slugs}
    for slug, (adm, mem) in PEOPLE.items():
        by_dept[slug] += [people_ids[adm[0]], people_ids[mem[0]]]
    for key, role in ROLES.items():
        by_dept[role.dept].append(agents[key]["participant_id"])
    for slug in slugs:
        name = f"{names[slug]} deal team"
        if name not in teams:
            call(
                admins[slug],
                "POST",
                "/v1/teams",
                {
                    "name": name,
                    "description": f"{names[slug]} people and agents on Project Falcon",
                    "member_ids": by_dept[slug],
                },
            )
    core = "Project Falcon core team"
    if core not in teams:
        lead_ids = [people_ids[PEOPLE[s][0][0]] for s in slugs]
        call(
            admin,
            "POST",
            "/v1/teams",
            {
                "name": core,
                "description": "One lead per department",
                "department_id": depts["finance"],
                "member_ids": lead_ids,
            },
        )

    print(f"room: {ROOM}")
    print(f"web:  {url.replace('8000', '5174')}   (password for everyone: {PASSWORD})")
    for slug, (adm, mem) in PEOPLE.items():
        print(f"  {names[slug]:<11} admin {emails[adm[0]]:<20} member {emails[mem[0]]}")
    for key, role in ROLES.items():
        peer = f"  consults @{role.peer}" if role.peer else ""
        print(f"  @{key:<18} {names[role.dept]:<11} port {role.port}  skill {role.skill}{peer}")


if __name__ == "__main__":
    main()
