"""Run the section 2 story end to end against a running Setu.

    make demo                       # Setu already running (make dev), admin from the env below
    make demo ARGS=--interactive    # the same world, but you drive it from the web app

What happens (all through Setu; the agents never talk to each other directly):
  1. The Risk and Compliance agents start as A2A servers, and a person per department is seeded.
  2. Olga (Operations) asks the Triage agent, in a shared room, to triage a claim.
  3. Triage delegates part of its authority to Risk and sends Risk a task in the room.
  4. A policy holds the task until a Compliance admin approves; Carl approves it.
  5. Setu forwards the task to Risk over A2A. Risk scores the claim and asks Compliance
     through Setu's A2A endpoint. One trace follows Triage -> Risk -> Compliance.
  6. Triage answers Olga; people in all three departments find the story in their inbox.

Env: SETU_URL (default http://localhost:8000), DEMO_ADMIN_EMAIL, DEMO_ADMIN_PASSWORD (an org
admin whose department is Operations, e.g. from `make bootstrap`).
"""

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from setu_sdk import SetuClient
from setu_sdk.a2a_serve import EmbeddedServer, free_port

from cross_department import compliance_agent, risk_agent, triage_agent
from cross_department.seed import Seeded, seed

REQUEST = "Triage claim #123, amount 12500"


@dataclass
class Story:
    seeded: Seeded
    reply: str
    task_states: list[str]
    trace_id: str
    trace: dict[str, Any]
    inboxes: dict[str, list[str]] = field(default_factory=dict)  # person -> notification summaries
    events: dict[str, list[str]] = field(default_factory=dict)  # person -> event types
    audit_ok: bool = False
    approvals: int = 0


async def _serve(app: Any, port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = EmbeddedServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            raise RuntimeError(f"could not start the server on port {port}")
        await asyncio.sleep(0.02)
    return server, task


async def _login(http: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
    r = await http.post("/v1/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _approve_when_asked(
    http: httpx.AsyncClient, headers: dict[str, str], done: asyncio.Event, count: list[int]
) -> None:
    """Carl: approve whatever is waiting for Compliance (stands in for the inbox UI)."""
    while not done.is_set():
        pending = (await http.get("/v1/approvals?state=pending", headers=headers)).json()
        for a in pending:
            r = await http.post(
                f"/v1/approvals/{a['id']}/decide",
                json={"approve": True, "note": "Approved by Compliance"},
                headers=headers,
            )
            if r.status_code == 200:
                count[0] += 1
                log(f"  Carl (Compliance) approved: {a['summary']}")
        await asyncio.sleep(0.3)


def log(line: str = "") -> None:
    print(line, flush=True)


@contextlib.asynccontextmanager
async def environment(
    setu_url: str,
    admin_email: str,
    admin_password: str,
    *,
    risk_port: int | None = None,
    compliance_port: int | None = None,
    require_approval: bool = True,
    max_wait: float = 120.0,
    say: Callable[[str], None] = lambda _l: None,
) -> AsyncIterator[Seeded]:
    """The demo world, up and running: Risk and Compliance serving A2A, Setu seeded (departments,
    people, agents, room, policies, a delegation) and the Triage agent listening in the room.
    Everything is stopped on exit."""
    risk_port, compliance_port = risk_port or free_port(), compliance_port or free_port()
    risk_url, compliance_url = (
        f"http://127.0.0.1:{risk_port}",
        f"http://127.0.0.1:{compliance_port}",
    )
    servers: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []
    triage: asyncio.Task[None] | None = None
    try:
        servers.append(await _serve(compliance_agent.build_app(compliance_url), compliance_port))
        # Risk's card must be reachable while it is registered, but its API key (for calling
        # Compliance through Setu) only exists afterwards: serve it now, fill the key in below.
        risk = risk_agent.RiskExecutor(setu_url, "", "")
        servers.append(await _serve(risk_agent.build_app(risk_url, risk), risk_port))

        say("Seeding departments, people, agents, room, policies and a delegation ...")
        s = await asyncio.to_thread(
            seed,
            setu_url,
            admin_email,
            admin_password,
            risk_url=risk_url,
            compliance_url=compliance_url,
            require_approval=require_approval,
        )
        risk.setu_api_key, risk.compliance_handle = s.risk["api_key"], s.compliance["handle"]
        say(f"  room {s.room_id}")
        say("  agents: triage (LangGraph via SDK), risk (native A2A), compliance (a2a_serve)")

        async with SetuClient(setu_url, s.triage["api_key"]) as triage_client:
            cfg = triage_agent.TriageConfig(
                s.risk["id"], s.root_delegation_id, wait_seconds=max_wait
            )
            agent = triage_agent.build_agent(triage_client, cfg, progress=True, discover_every=0.3)
            triage = asyncio.create_task(agent.run())
            yield s
    finally:
        if triage is not None:
            triage.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await triage
        for server, task in servers:
            server.should_exit = True
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 10)


async def run(
    setu_url: str,
    admin_email: str,
    admin_password: str,
    *,
    risk_port: int | None = None,
    compliance_port: int | None = None,
    require_approval: bool = True,
    request: str = REQUEST,
    max_wait: float = 120.0,
    quiet: bool = False,
) -> Story:
    """The scripted story: Olga asks, Carl approves, everything is checked and printed."""
    say = (lambda _l="": None) if quiet else log
    async with (
        environment(
            setu_url,
            admin_email,
            admin_password,
            risk_port=risk_port,
            compliance_port=compliance_port,
            require_approval=require_approval,
            max_wait=max_wait,
            say=say,
        ) as s,
        httpx.AsyncClient(base_url=setu_url, timeout=30) as http,
    ):
        olga = await _login(http, s.emails["olga"], s.password)
        carl = await _login(http, s.emails["carl"], s.password)
        ada = await _login(http, s.emails["ada"], s.password)
        rita = await _login(http, s.emails["rita"], s.password)

        done = asyncio.Event()
        approvals = [0]
        approver = asyncio.create_task(_approve_when_asked(http, carl, done, approvals))
        try:
            say(f"\nOlga (Operations) to Triage-Agent: {request!r}")
            sent = (
                await http.post(
                    f"/v1/rooms/{s.room_id}/messages",
                    json={"body": {"text": request}, "mentions": [s.triage["id"]]},
                    headers=olga,
                )
            ).json()
            reply = await _wait_for_reply(http, olga, s, sent["seq"], max_wait)
        finally:
            done.set()
            approver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await approver
        say(f"Triage-Agent to Olga: {reply}\n")

        tasks = (await http.get(f"/v1/rooms/{s.room_id}/tasks", headers=olga)).json()
        trace_id = tasks[0]["trace_id"] if tasks else ""
        trace: dict[str, Any] = {"roots": []}
        if trace_id:
            trace = (await http.get(f"/v1/traces/{trace_id}", headers=ada)).json()
            for line in _render_trace(trace["roots"]):
                say(line)

        story = Story(
            s, reply, [t["state"] for t in tasks], trace_id, trace, approvals=approvals[0]
        )
        await asyncio.sleep(0.5)  # let the last notifications commit
        for who, hdr in (("olga", olga), ("rita", rita), ("carl", carl)):
            items = (await http.get("/v1/notifications?limit=100", headers=hdr)).json()["items"]
            story.inboxes[who] = [n["summary"] for n in reversed(items)]
            story.events[who] = [n["event_type"] for n in reversed(items)]
            say(f"\n{who.capitalize()}'s inbox:")
            for n in reversed(items):
                say(f"  - {n['summary']}")
        story.audit_ok = (await http.post("/v1/audit/verify", headers=ada)).json()["ok"]
        say(f"\nAudit chain intact: {story.audit_ok}")
        return story


async def interactive(
    setu_url: str,
    admin_email: str,
    admin_password: str,
    *,
    web_url: str = "http://localhost:5173",
    risk_port: int | None = None,
    compliance_port: int | None = None,
    require_approval: bool = True,
    ready: Callable[[Seeded], None] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Set the world up and hand it to people: nothing is scripted. Sign in to the web app as
    each person and run the story yourself. Returns when `stop` is set (or on Ctrl-C)."""
    async with environment(
        setu_url,
        admin_email,
        admin_password,
        risk_port=risk_port,
        compliance_port=compliance_port,
        require_approval=require_approval,
        say=log,
    ) as s:
        log(f"\nOpen {web_url}/#/rooms/{s.room_id}")
        log(f"Everyone's password: {s.password}\n")
        for who, where in (
            ("olga", "Operations: asks Triage to triage a claim"),
            ("rita", "Risk"),
            ("carl", "Compliance: approves the risk step from the inbox"),
            ("ada", "auditor: audit log and full traces"),
        ):
            log(f"  {s.emails[who]:<28} {where}")
        log(f"\nIn the room, as Olga, send: @triage-{s.tag} {REQUEST}\nCtrl-C to stop.")
        if ready:
            ready(s)
        await (stop or asyncio.Event()).wait()


async def _wait_for_reply(
    http: httpx.AsyncClient, headers: dict[str, str], s: Seeded, after_seq: int, max_wait: float
) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        rows = (
            await http.get(
                f"/v1/rooms/{s.room_id}/messages?after_seq={after_seq}&limit=500", headers=headers
            )
        ).json()
        for m in rows:
            if m["sender_id"] == s.triage["id"] and m["kind"] == "text":
                return str(m["body"]["text"])
        await asyncio.sleep(0.3)
    raise TimeoutError("Triage-Agent did not answer in time")


def _render_trace(roots: list[dict[str, Any]], depth: int = 0) -> list[str]:
    lines = [] if depth else ["Trace (who asked whom, which policy allowed it, delegated scope):"]
    for n in roots:
        a = n["attributes"]
        extra = ""
        if n["kind"] == "policy":
            extra = f" -> {a.get('decision')} ({a.get('reason')})"
        elif n["kind"] == "delegation":
            extra = f" -> chain of {len(a.get('chain', []))}, scopes {a.get('scopes')}"
        elif n["kind"] == "a2a_call":
            extra = f" -> {n['status']} in {n.get('duration_ms')} ms"
        elif n["kind"] == "approval":
            extra = f" -> {n['status']}"
        lines.append(f"  {'  ' * depth}{n['kind']}: {n['name']}{extra}")
        lines += _render_trace(n["children"], depth + 1)
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--setu-url", default=os.environ.get("SETU_URL", "http://localhost:8000"))
    ap.add_argument(
        "--no-approval", action="store_true", help="skip the Compliance sign-off policy"
    )
    ap.add_argument("--risk-port", type=int, default=9101)
    ap.add_argument("--compliance-port", type=int, default=9102)
    ap.add_argument("--request", default=REQUEST)
    ap.add_argument(
        "--interactive",
        action="store_true",
        help="set everything up and wait: you play Olga, Carl and Ada in the web app",
    )
    ap.add_argument("--web-url", default=os.environ.get("WEB_URL", "http://localhost:5173"))
    args = ap.parse_args()
    email, password = os.environ.get("DEMO_ADMIN_EMAIL"), os.environ.get("DEMO_ADMIN_PASSWORD")
    if not email or not password:
        sys.exit(
            "Set DEMO_ADMIN_EMAIL and DEMO_ADMIN_PASSWORD (an org admin; see `make bootstrap`)."
        )
    if args.interactive:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(
                interactive(
                    args.setu_url,
                    email,
                    password,
                    web_url=args.web_url,
                    risk_port=args.risk_port,
                    compliance_port=args.compliance_port,
                    require_approval=not args.no_approval,
                )
            )
        return
    asyncio.run(
        run(
            args.setu_url,
            email,
            password,
            risk_port=args.risk_port,
            compliance_port=args.compliance_port,
            require_approval=not args.no_approval,
            request=args.request,
        )
    )


if __name__ == "__main__":
    main()
