"""Seed Setu with the cross-department demo, through the public API only.

Creates (idempotently where it matters): the Risk and Compliance departments next to the
existing Operations one; people (Olga in Ops, Rita in Risk, Carl in Compliance, Ada the
auditor); the Triage agent (an SDK agent with an API key); the Risk and Compliance agents,
registered by Agent Card URL; one shared room; the policies that make the story interesting;
and a delegation from Olga to the Triage agent.

    seed(setu_url, admin_email, admin_password, risk_url=..., compliance_url=...)
"""

import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

DEMO_PASSWORD = "demo-password-123"  # for the demo people only; never use outside a dev setup


@dataclass
class Seeded:
    setu_url: str
    tag: str
    password: str
    emails: dict[str, str]  # olga, rita, carl, ada
    departments: dict[str, str]  # ops, risk, compliance -> id
    triage: dict[str, str]  # id, handle, api_key
    risk: dict[str, str]  # id, handle, api_key (it calls Setu's facade)
    compliance: dict[str, str]  # id, handle
    room_id: str
    root_delegation_id: str
    policies: list[str] = field(default_factory=list)


class Api:
    """Tiny sync client for admin-style setup calls."""

    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=30)
        self.headers: dict[str, str] = {}

    def login(self, email: str, password: str) -> None:
        r = self.http.post("/v1/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        self.headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        self.me: dict[str, Any] = r.json()["participant"]

    def call(
        self, method: str, path: str, body: Any = None, *, ok: tuple[int, ...] = (200, 201)
    ) -> Any:
        r = self.http.request(method, path, json=body, headers=self.headers)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json() if r.content else None

    def person(self, email: str, password: str) -> dict[str, str]:
        r = self.http.post("/v1/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        return {"token": r.json()["access_token"], "id": r.json()["participant"]["id"]}


def seed(
    setu_url: str,
    admin_email: str,
    admin_password: str,
    *,
    risk_url: str,
    compliance_url: str,
    tag: str | None = None,
    require_approval: bool = True,
) -> Seeded:
    tag = tag or secrets.token_hex(2)
    api = Api(setu_url)
    api.login(admin_email, admin_password)
    ops_id = api.me["department_id"]  # the admin's department is Operations

    depts = {"ops": ops_id}
    existing = {d["slug"]: d["id"] for d in api.call("GET", "/v1/departments")}
    for slug, name in (("risk", "Risk"), ("compliance", "Compliance")):
        depts[slug] = (
            existing.get(slug)
            or api.call("POST", "/v1/departments", {"name": name, "slug": slug})["id"]
        )

    emails: dict[str, str] = {}
    for who, dept, roles in (
        ("olga", "ops", ["dept_member"]),
        ("rita", "risk", ["dept_admin"]),
        ("carl", "compliance", ["dept_admin"]),
        ("ada", "ops", ["auditor"]),
    ):
        emails[who] = f"{who}-{tag}@demo.test"
        api.call(
            "POST",
            "/v1/humans",
            {
                "department_id": depts[dept],
                "display_name": who.capitalize(),
                "handle": f"{who}-{tag}",
                "email": emails[who],
                "password": DEMO_PASSWORD,
                "roles": roles,
            },
        )

    # Operations' Triage agent is an SDK agent: it connects to Setu with an API key.
    triage = api.call(
        "POST",
        "/v1/participants",
        {"department_id": depts["ops"], "display_name": "Triage-Agent", "handle": f"triage-{tag}"},
    )
    triage_key = api.call("POST", f"/v1/participants/{triage['id']}/api-keys", {})["key"]

    # Risk and Compliance are A2A agents: each team pastes its Agent Card URL. If that card is
    # already registered (a re-run, same ports), reuse that agent instead of failing.
    def register(url: str, dept: str, handle: str) -> dict[str, Any]:
        body = {"card_url": url + "/.well-known/agent-card.json", "department_id": dept}
        r = api.http.post(
            "/v1/agents/register", json={**body, "handle": handle}, headers=api.headers
        )
        if r.status_code == 409 and "already registered" in r.text:
            for a in api.call("GET", "/v1/agents"):  # admins see each agent's card URL
                if a.get("card_url") == body["card_url"]:
                    out: dict[str, Any] = a
                    return out
        if r.status_code != 201:
            raise RuntimeError(f"POST /v1/agents/register -> {r.status_code}: {r.text}")
        out = r.json()
        return out

    risk = register(risk_url, depts["risk"], f"risk-{tag}")
    risk_key = api.call("POST", f"/v1/participants/{risk['participant_id']}/api-keys", {})["key"]
    compliance = register(compliance_url, depts["compliance"], f"compliance-{tag}")

    # Olga opens a cross-department room and adds the three agents.
    olga = Api(setu_url)
    olga.login(emails["olga"], DEMO_PASSWORD)
    room = olga.call(
        "POST", "/v1/rooms", {"name": f"claims-{tag}", "visibility": "cross_department"}
    )
    for pid in (triage["id"], risk["participant_id"], compliance["participant_id"]):
        olga.call("POST", f"/v1/rooms/{room['id']}/members", {"participant_id": pid})

    # Policy: cross-department traffic is denied until a rule allows it.
    policies: list[str] = []
    ops, risk_d, comp = depts["ops"], depts["risk"], depts["compliance"]
    rules: list[tuple[str, dict[str, Any]]] = [
        (
            "Ops may ask Risk",
            {"type": "allow_talk", "from_department": ops, "to_department": risk_d},
        ),
        (
            "Risk may ask Compliance",
            {"type": "allow_talk", "from_department": risk_d, "to_department": comp},
        ),
    ]
    if require_approval:
        rules.append(
            (
                "Risk scoring needs Compliance sign-off",
                {
                    "type": "require_approval",
                    "skill": "risk.score",
                    "approver_department": depts["compliance"],
                    "approver_role": "dept_admin",
                },
            )
        )
    have = {p["name"]: p for p in api.call("GET", "/v1/policies")}
    for name, rule in rules:
        if name not in have:
            api.call("POST", "/v1/policies", {"name": name, "rule": rule})
        policies.append(name)

    # Olga lets the Triage agent act for her, for exactly these skills.
    root = olga.call(
        "POST",
        "/v1/delegations",
        {
            "to_participant_id": triage["id"],
            "scopes": ["skill:risk.score", "skill:compliance.check"],
        },
    )

    return Seeded(
        setu_url=setu_url,
        tag=tag,
        password=DEMO_PASSWORD,
        emails=emails,
        departments=depts,
        triage={"id": triage["id"], "handle": triage["handle"], "api_key": triage_key},
        risk={"id": risk["participant_id"], "handle": risk["handle"], "api_key": risk_key},
        compliance={"id": compliance["participant_id"], "handle": compliance["handle"]},
        room_id=room["id"],
        root_delegation_id=root["id"],
        policies=policies,
    )
