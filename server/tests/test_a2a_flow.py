"""Phase 4 acceptance: register agents by Agent Card URL; a room task reaches the Risk agent,
streams updates back as messages, and ends with an artifact. Against A2A 1.0 and 0.3 agents."""

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.a2a import mapping
from app.db.session import make_engine
from app.main import create_app
from tests.a2a_agents import HostRouter, Skill, build_v03_agent, build_v1_agent
from tests.helpers import Acme, Person

RISK = "http://risk.test"
OPS = "http://ops.test"
SKILLS: list[Skill] = [("risk.score", "Risk scoring", ["risk", "claims"])]

AgentBuilder = Callable[..., FastAPI]
BUILDERS = {"v1": build_v1_agent, "v03": build_v03_agent}


@contextmanager
def hub(url: str, apps: dict[str, FastAPI]) -> Iterator[tuple[Acme, HostRouter, TestClient]]:
    router = HostRouter(dict(apps))
    with TestClient(create_app(url, a2a_transport=router)) as client:
        yield Acme(client, url), router, client


def wait_for(fn: Callable[[], Any], timeout: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def register(
    w: Acme, who: Person, card_url: str, dept: dict[str, Any], **kw: Any
) -> dict[str, Any]:
    return w.ok(
        w.post(
            "/v1/agents/register", who, {"card_url": card_url, "department_id": dept["id"], **kw}
        )
    )


class Scene:
    """Ops human + registered Risk agent in a cross-department room."""

    def __init__(
        self,
        w: Acme,
        card_url: str = RISK + "/.well-known/agent-card.json",
        allow: bool = True,
        **reg: Any,
    ) -> None:
        self.w = w
        if allow:  # cross-department traffic is denied until a rule allows it
            w.allow_talk(w.ops, w.risk)
        self.olga = w.mk_human(w.ops, "olga")
        self.risk_admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        self.agent = register(
            w, self.risk_admin, card_url, w.risk, handle=f"risk-agent-{w.tag}", **reg
        )
        self.pid = self.agent["participant_id"]
        self.room = w.mk_room(olga := self.olga, "claims-123", visibility="cross_department")
        w.add_member(self.room, olga, self.pid)
        self.rid = self.room["id"]

    def send_task(self, text: str, **extra: Any) -> dict[str, Any]:
        r = self.w.post(
            f"/v1/rooms/{self.rid}/messages",
            self.olga,
            {
                "kind": "task",
                "body": {"text": text, "skill": "risk.score"},
                "mentions": [self.pid],
                **extra,
            },
        )
        return self.w.ok(r)

    def tasks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = self.w.get(f"/v1/rooms/{self.rid}/tasks", self.olga).json()
        return out

    def task_when(self, state: str, n: int = 0) -> dict[str, Any]:
        def check() -> dict[str, Any] | None:
            ts = sorted(self.tasks(), key=lambda t: t["created_at"])
            return ts[n] if len(ts) > n and ts[n]["state"] == state else None

        got: dict[str, Any] = wait_for(check, what=f"task {n} to reach {state}")
        return got

    def messages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = self.w.get(
            f"/v1/rooms/{self.rid}/messages?limit=500", self.olga
        ).json()
        return out


# ------------------------------------------------------------------ registration & discovery


@pytest.mark.parametrize("flavor", ["v1", "v03"])
def test_register_by_card_url_and_discover_by_skill(
    migrated_database_url: str, flavor: str
) -> None:
    app = BUILDERS[flavor](RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, router, _):
        admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        reg = register(
            w, admin, RISK + "/.well-known/agent-card.json", w.risk, handle=f"risk-{w.tag}"
        )
        assert reg["transport"] == "a2a_remote" and reg["streaming"] is True
        assert reg["a2a_version"] == ("1.0" if flavor == "v1" else "0.3")
        assert reg["skills"][0]["id"] == "risk.score"
        assert reg["rpc_url"] == RISK + "/rpc" and reg["has_credentials"] is False
        assert reg["department_id"] == w.risk["id"]

        stranger = w.mk_human(w.ops, "olga")
        found = w.get("/v1/agents?skill=risk.score", stranger).json()
        assert [a["participant_id"] for a in found] == [reg["participant_id"]]
        assert [a["handle"] for a in w.get("/v1/agents?skill=claims", stranger).json()] == [
            f"risk-{w.tag}"
        ]
        assert w.get("/v1/agents?skill=nope", stranger).json() == []
        assert len(w.get("/v1/agents?q=SCORING", stranger).json()) == 1
        # Other departments never see the real endpoint or registration details.
        public = w.get(f"/v1/agents/{reg['participant_id']}", stranger).json()
        assert (
            "rpc_url" not in public and "card_url" not in public and "has_credentials" not in public
        )
        assert "rpc_url" in w.get(f"/v1/agents/{reg['participant_id']}", admin).json()
        # The registered agent is a participant owned by the department.
        people = w.get(f"/v1/participants?department_id={w.risk['id']}", stranger).json()
        assert reg["participant_id"] in [p["id"] for p in people]


def test_register_with_base_url_and_legacy_card_path(migrated_database_url: str) -> None:
    app = build_v03_agent(
        "http://old.test", name="Legacy", skills=SKILLS, card_path="/.well-known/agent.json"
    )
    with hub(migrated_database_url, {"old.test": app}) as (w, router, _):
        admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        reg = register(w, admin, "http://old.test", w.risk, handle=f"legacy-{w.tag}")
        assert reg["a2a_version"] == "0.3"
        paths = [p for _, _, p in router.calls]
        assert "/.well-known/agent-card.json" in paths and "/.well-known/agent.json" in paths


def test_registration_rules_and_errors(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, router, client):
        url = RISK + "/.well-known/agent-card.json"
        member = w.mk_human(w.risk, "mia")
        assert (
            w.post(
                "/v1/agents/register", member, {"card_url": url, "department_id": w.risk["id"]}
            ).status_code
            == 403
        )
        ops_admin = w.mk_human(w.ops, "oz", ["dept_admin"])
        assert (
            w.post(
                "/v1/agents/register", ops_admin, {"card_url": url, "department_id": w.risk["id"]}
            ).status_code
            == 403
        )
        admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        assert w.post("/v1/agents/register", None, {"card_url": url}).status_code == 401
        register(w, admin, url, w.risk, handle=f"r1-{w.tag}")
        dup = w.post(
            "/v1/agents/register",
            admin,
            {"card_url": url, "department_id": w.risk["id"], "handle": f"r2-{w.tag}"},
        )
        assert dup.status_code == 409
        for bad, code in [
            ("http://nowhere.test/.well-known/agent-card.json", 502),
            ("http://risk.test/nothing", 502),
            ("ftp://x.test/card", 422),
        ]:
            r = w.post(
                "/v1/agents/register", admin, {"card_url": bad, "department_id": w.risk["id"]}
            )
            assert r.status_code == code, (bad, r.text)
        # A reachable URL that is not an agent card.
        router.apps["risk.test"].add_api_route("/junk", lambda: {"hello": "world"})
        r = w.post(
            "/v1/agents/register",
            admin,
            {"card_url": RISK + "/junk", "department_id": w.risk["id"]},
        )
        assert r.status_code == 422 and "card" in r.json()["detail"]
        # Handle collision is a clear 409, not a crash.
        other = build_v1_agent("http://other.test", name="Risk", skills=SKILLS)
        router.apps["other.test"] = other
        clash = w.post(
            "/v1/agents/register",
            admin,
            {
                "card_url": "http://other.test/.well-known/agent-card.json",
                "department_id": w.risk["id"],
                "handle": f"r1-{w.tag}",
            },
        )
        assert clash.status_code == 409 and "handle" in clash.json()["detail"]
        ok = w.post(
            "/v1/agents/register",
            admin,
            {
                "card_url": "http://other.test/.well-known/agent-card.json",
                "department_id": w.risk["id"],
                "handle": f"r3-{w.tag}",
            },
        )
        assert ok.status_code == 201


def test_card_refresh_detects_changes(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        reg = register(w, admin, RISK + "/.well-known/agent-card.json", w.risk, handle=f"r-{w.tag}")
        url = f"/v1/agents/{reg['participant_id']}/refresh"
        assert w.ok(w.post(url, admin, {}), 200)["changed"] is False
        app.state.skills = [*SKILLS, ("risk.explain", "Explain", ["risk"])]
        out = w.ok(w.post(url, admin, {}), 200)
        assert out["changed"] is True and {s["id"] for s in out["skills"]} == {
            "risk.score",
            "risk.explain",
        }
        assert w.ok(w.post(url, admin, {}), 200)["changed"] is False
        assert len(w.get("/v1/agents?skill=risk.explain", admin).json()) == 1
        outsider = w.mk_human(w.ops, "olga")
        assert w.post(url, outsider, {}).status_code == 403


# ------------------------------------------------------------------ the acceptance flow


@pytest.mark.parametrize("flavor", ["v1", "v03"])
def test_room_task_reaches_agent_streams_back_and_ends_with_artifact(
    migrated_database_url: str, flavor: str
) -> None:
    risk = BUILDERS[flavor](RISK, name="Risk", skills=SKILLS)
    ops = BUILDERS[flavor](OPS, name="Ops", skills=[("claims.triage", "Triage", ["claims"])])
    with hub(migrated_database_url, {"risk.test": risk, "ops.test": ops}) as (w, router, _):
        s = Scene(w)
        ops_admin = w.mk_human(w.ops, "oscar", ["dept_admin"])
        ops_reg = register(
            w, ops_admin, OPS + "/.well-known/agent-card.json", w.ops, handle=f"ops-agent-{w.tag}"
        )
        w.add_member(
            s.room, s.olga, ops_reg["participant_id"]
        )  # both example agents are in the room

        sent = s.send_task("score claim 123")
        assert sent["addressed"] == [s.pid]  # only the Risk agent was asked
        task = s.task_when("completed")

        # The agent saw exactly one call, with the room as the A2A context and a stable message id.
        assert len(risk.state.received) == 1 and not ops.state.received
        rec = risk.state.received[0]
        wire_msg = rec.body["params"]["message"]
        assert wire_msg["contextId"] == s.rid
        assert wire_msg["messageId"] == mapping.message_id_for_task(uuid.UUID(task["id"]))
        assert rec.method == ("SendStreamingMessage" if flavor == "v1" else "message/stream")
        assert ("a2a-version" in rec.headers) == (flavor == "v1")
        # One trace id follows the task: it is in the W3C header and mirrored in metadata.
        trace = task["trace_id"]
        assert rec.headers["traceparent"].split("-")[1] == trace and len(trace) == 32
        assert wire_msg["metadata"]["setu"]["trace_id"] == trace

        # The room shows the whole story, from the agent's own participant, in order.
        msgs = [m for m in s.messages() if m["task_id"] == task["id"]]
        kinds = [(m["kind"], m["body"].get("state")) for m in msgs]
        assert kinds == [
            ("task", None),
            ("task_update", "working"),
            ("result", None),
            ("task_update", "completed"),
        ]
        assert [m["sender_id"] for m in msgs[1:]] == [s.pid] * 3
        result = msgs[2]["body"]
        assert result["text"] == "Risk handled: score claim 123" and result["name"] == "result"
        assert all(m["trace_id"] == trace for m in msgs)
        # ...addressed back to the requester, so Olga's inbox has the updates.
        assert all(s.olga.id in m["addressed"] for m in msgs[1:])

        # The task row carries the A2A ids and the artifact.
        assert task["assignee_id"] == s.pid and task["requester_id"] == s.olga.id
        assert task["a2a_task_id"] and task["a2a_context_id"] == s.rid and task["completed_at"]
        assert [a["parts"] for a in task["artifacts"]] == [
            [{"text": "Risk handled: score claim 123"}]
        ]
        assert w.get(f"/v1/tasks/{task['id']}", s.olga).json()["state"] == "completed"
        # Messages were announced in seq order with no gaps.
        assert [m["seq"] for m in s.messages()] == list(range(1, len(s.messages()) + 1))


def test_multiple_artifacts_each_become_a_result_message(migrated_database_url: str) -> None:
    with hub(
        migrated_database_url, {"risk.test": build_v1_agent(RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, _):
        s = Scene(w)
        s.send_task("multi please")
        task = s.task_when("completed")
        assert [a["name"] for a in task["artifacts"]] == ["part-1", "part-2"]
        results = [m for m in s.messages() if m["kind"] == "result"]
        assert [m["body"]["name"] for m in results] == ["part-1", "part-2"]


@pytest.mark.parametrize("flavor", ["v1", "v03"])
def test_agent_failure_is_visible(migrated_database_url: str, flavor: str) -> None:
    with hub(
        migrated_database_url, {"risk.test": BUILDERS[flavor](RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, _):
        s = Scene(w)
        s.send_task("fail now")
        task = s.task_when("failed")
        upd = [
            m
            for m in s.messages()
            if m["task_id"] == task["id"] and m["body"].get("state") == "failed"
        ]
        assert len(upd) == 1 and upd[0]["body"]["text"] == "boom"
        assert not task["artifacts"] and task["completed_at"]


@pytest.mark.parametrize("flavor", ["v1", "v03"])
def test_input_required_becomes_a_question_for_the_requester(
    migrated_database_url: str, flavor: str
) -> None:
    with hub(
        migrated_database_url, {"risk.test": BUILDERS[flavor](RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, _):
        s = Scene(w)
        s.send_task("input needed")
        task = s.task_when("input_required")
        q = [m for m in s.messages() if m["kind"] == "approval_request"]
        assert len(q) == 1 and q[0]["body"]["prompt"] == "which claim id?"
        assert q[0]["task_id"] == task["id"] and s.olga.id in q[0]["addressed"]
        assert task["completed_at"] is None  # interrupted, not finished


@pytest.mark.parametrize("flavor", ["v1", "v03"])
def test_cancel_sends_cancel_to_the_agent(migrated_database_url: str, flavor: str) -> None:
    app = BUILDERS[flavor](RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        s.send_task("slow job")
        task = s.task_when("working")
        stranger = w.mk_human(w.risk, "sam")
        assert (
            w.post(f"/v1/tasks/{task['id']}/cancel", stranger, {}).status_code == 404
        )  # cannot see it
        out = w.ok(w.post(f"/v1/tasks/{task['id']}/cancel", s.olga, {}), 200)
        assert out["state"] == "canceled"
        methods = [r.method for r in app.state.received]
        assert ("CancelTask" if flavor == "v1" else "tasks/cancel") in methods
        assert w.post(f"/v1/tasks/{task['id']}/cancel", s.olga, {}).status_code == 409
        time.sleep(0.3)  # the mirrored stream must not resurrect it
        assert w.get(f"/v1/tasks/{task['id']}", s.olga).json()["state"] == "canceled"
        notes = [m for m in s.messages() if m["body"].get("state") == "canceled"]
        assert len(notes) == 1 and notes[0]["sender_id"] == s.olga.id


# ------------------------------------------------------------------ failure handling


def test_unreachable_agent_is_retried_then_fails_visibly(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, router, _):
        s = Scene(w, max_retries=2)
        router.down.add("risk.test")
        s.send_task("score claim 9")
        task = s.task_when("failed")
        attempts = [c for c in router.calls if c[1] == "POST" and c[2] == "/rpc"]
        assert len(attempts) == 3  # first try + 2 retries
        assert "transport error" in task["error"] or "ConnectError" in task["error"]
        failed = [m for m in s.messages() if m["body"].get("state") == "failed"]
        assert len(failed) == 1 and failed[0]["body"]["origin"] == "hub"
        assert failed[0]["body"]["error"] == task["error"]
        assert not app.state.received


def test_zero_retries_and_non_retryable_errors_do_not_retry(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS, bearer_token="s3cret")
    with hub(migrated_database_url, {"risk.test": app}) as (w, router, _):
        s = Scene(w, max_retries=3)  # registered WITHOUT credentials: the agent answers 401
        assert s.agent["warnings"] and "security schemes" in s.agent["warnings"][0]
        s.send_task("score claim 1")
        task = s.task_when("failed")
        assert "401" in task["error"]
        assert len([c for c in router.calls if c[2] == "/rpc"]) == 1  # 401 is not retried


def test_credentials_are_sent_encrypted_at_rest_and_never_returned(
    migrated_database_url: str,
) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS, bearer_token="tok-12345")
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w, auth={"type": "bearer", "token": "tok-12345"})
        assert (
            s.agent["has_credentials"] is True and "warnings" in s.agent and not s.agent["warnings"]
        )
        s.send_task("score claim 2")
        s.task_when("completed")
        assert app.state.received[0].headers["authorization"] == "Bearer tok-12345"
        # Not in any API response...
        everything = [
            w.get(f"/v1/agents/{s.pid}", s.risk_admin).text,
            w.get("/v1/agents", s.risk_admin).text,
            str(s.agent),
            w.get(f"/v1/rooms/{s.rid}/tasks", s.olga).text,
            w.get(f"/v1/rooms/{s.rid}/messages", s.olga).text,
        ]
        assert not any("tok-12345" in blob for blob in everything)

        # ...and not in the database in clear text.
        async def stored() -> str:
            eng = make_engine(migrated_database_url)
            try:
                async with eng.connect() as c:
                    row = (
                        await c.execute(
                            text("select auth_config::text from agents where participant_id=:p"),
                            {"p": s.pid},
                        )
                    ).scalar_one()
                    return str(row)
            finally:
                await eng.dispose()

        blob = asyncio.run(stored())
        assert "tok-12345" not in blob and '"ct"' in blob
        # Rotating credentials takes effect on the next call.
        app.state.received.clear()
        bad = w.post  # noqa: F841
        r = w.c.patch(
            f"/v1/agents/{s.pid}",
            headers=s.risk_admin.headers,
            json={"auth": {"type": "bearer", "token": "wrong"}},
        )
        assert r.status_code == 200
        s.send_task("score claim 3")
        s.task_when("failed", 1)


def test_api_key_scheme_is_supported(migrated_database_url: str) -> None:
    from tests.a2a_agents import build_v03_agent as v03

    app = v03(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w, auth={"type": "api_key", "header": "X-Agent-Key", "value": "k-1"})
        s.send_task("score claim 4")
        s.task_when("completed")
        assert app.state.received[0].headers["x-agent-key"] == "k-1"


def test_auth_input_validation(migrated_database_url: str) -> None:
    with hub(
        migrated_database_url, {"risk.test": build_v1_agent(RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, _):
        admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        for auth in ({"type": "bearer"}, {"type": "api_key", "header": "X"}, {"type": "basic"}):
            r = w.post(
                "/v1/agents/register",
                admin,
                {
                    "card_url": RISK + "/.well-known/agent-card.json",
                    "department_id": w.risk["id"],
                    "auth": auth,
                },
            )
            assert r.status_code == 422, auth


# ------------------------------------------------------------------ routing, threads, visibility


def test_followup_in_task_thread_addresses_the_assignee_and_child_tasks_link(
    migrated_database_url: str,
) -> None:
    with hub(
        migrated_database_url, {"risk.test": build_v1_agent(RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, _):
        s = Scene(w)
        first = s.send_task("score claim 5")
        task = s.task_when("completed")
        reply = w.ok(
            w.post(
                f"/v1/rooms/{s.rid}/messages",
                s.olga,
                {"body": {"text": "thanks, one more question"}, "task_id": task["id"]},
            )
        )
        assert reply["addressed"] == [
            s.pid
        ]  # no mention needed: the thread's assignee is addressed
        child = s.send_task("now rescore", task_id=task["id"])
        s.task_when("completed", 1)
        tasks = sorted(s.tasks(), key=lambda t: t["created_at"])
        assert tasks[1]["parent_task_id"] == task["id"] and tasks[1]["trace_id"] == task["trace_id"]
        assert first["task_id"] == task["id"] and child["task_id"] == tasks[1]["id"]
        bogus = w.post(
            f"/v1/rooms/{s.rid}/messages",
            s.olga,
            {"body": {"text": "x"}, "task_id": str(uuid.uuid4())},
        )
        assert bogus.status_code == 422


def test_task_visibility_follows_room_access(migrated_database_url: str) -> None:
    with hub(
        migrated_database_url, {"risk.test": build_v1_agent(RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, _):
        s = Scene(w)
        s.send_task("score claim 6")
        task = s.task_when("completed")
        outsider = w.mk_human(w.compliance, "carl")
        assert w.get(f"/v1/tasks/{task['id']}", outsider).status_code == 404
        assert w.get(f"/v1/rooms/{s.rid}/tasks", outsider).status_code == 404
        r = w.c.put(
            f"/v1/rooms/{s.rid}/access",
            headers=s.olga.headers,
            json={"department_id": w.compliance["id"], "access": "read"},
        )
        assert r.status_code == 200
        assert w.get(f"/v1/tasks/{task['id']}", outsider).status_code == 200


def test_task_messages_only_create_tasks_for_enabled_a2a_agents(migrated_database_url: str) -> None:
    with hub(
        migrated_database_url, {"risk.test": build_v1_agent(RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, _):
        s = Scene(w)
        admin_view = w.c.patch(
            f"/v1/agents/{s.pid}", headers=s.risk_admin.headers, json={"enabled": False}
        )
        assert admin_view.status_code == 200 and admin_view.json()["enabled"] is False
        s.send_task("score claim 7")
        time.sleep(0.4)
        assert s.tasks() == []  # disabled: the message stands, no task is created
        # SDK-style agents (WebSocket) are addressed but never dispatched over A2A.
        sdk = w.mk_agent(w.risk, "sdk-bot")
        w.add_member(s.room, s.olga, sdk["id"])
        r = w.ok(
            w.post(
                f"/v1/rooms/{s.rid}/messages",
                s.olga,
                {"kind": "task", "body": {"text": "hi"}, "mentions": [sdk["id"]]},
            )
        )
        assert r["addressed"] == [sdk["id"]] and s.tasks() == []


def test_text_and_data_both_reach_the_agent(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        w.ok(
            w.post(
                f"/v1/rooms/{s.rid}/messages",
                s.olga,
                {
                    "kind": "task",
                    "mentions": [s.pid],
                    "body": {"text": "score", "data": {"claim": 123}},
                },
            )
        )
        s.task_when("completed")
        assert app.state.received[0].body["params"]["message"]["parts"] == [
            {"text": "score"},
            {"data": {"claim": 123}},
        ]


def test_websocket_task_message_also_dispatches(migrated_database_url: str) -> None:
    with hub(
        migrated_database_url, {"risk.test": build_v1_agent(RISK, name="Risk", skills=SKILLS)}
    ) as (w, _, client):
        s = Scene(w)
        with client.websocket_connect(f"/v1/ws/rooms/{s.rid}?token={s.olga.token}") as ws:
            ws.send_json(
                {"type": "send", "kind": "task", "mentions": [s.pid], "body": {"text": "score 8"}}
            )
            seen: list[dict[str, Any]] = []
            terminal = ("completed", "failed", "canceled")
            while not any(m.get("body", {}).get("state") in terminal for m in seen):
                frame = ws.receive_json()
                if frame["type"] == "message":
                    seen.append(frame["message"])
        assert [m["kind"] for m in seen] == ["task", "task_update", "result", "task_update"]
        assert seen[-1]["body"]["state"] == "completed"


# ------------------------------------------------------------------ hub restart


def test_hub_restart_fails_orphaned_task_visibly(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        s.send_task("slow forever")
        task = s.task_when("working")
        ids = (s.rid, task["id"], s.olga)
    # The first hub is gone mid-task. A new hub on the same database cleans up after it.
    router = HostRouter({"risk.test": app})
    with TestClient(create_app(migrated_database_url, a2a_transport=router)) as client:

        def check() -> Any:
            r = client.get(f"/v1/tasks/{ids[1]}", headers=ids[2].headers)
            return r.json() if r.json()["state"] == "failed" else None

        failed = wait_for(check, what="orphaned task to be failed")
        assert "restarted" in failed["error"]
        msgs = client.get(f"/v1/rooms/{ids[0]}/messages?limit=100", headers=ids[2].headers).json()
        assert any(
            m["body"].get("origin") == "hub" and m["body"].get("state") == "failed" for m in msgs
        )
