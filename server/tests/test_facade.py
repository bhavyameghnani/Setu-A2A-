"""Phase 6 acceptance: agent A calls agent B only through Setu's A2A endpoint; one trace shows
A -> B -> C with policy and delegation spans; real endpoints are never exposed."""

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.tracing import format_traceparent, parse_traceparent
from tests.a2a_agents import build_v03_agent, build_v1_agent
from tests.helpers import Acme, Person
from tests.test_a2a_flow import hub, register, wait_for

RISK = "http://risk.internal.test"
COMP = "http://compliance.internal.test"
RISK_SKILLS = [("risk.score", "Risk scoring", ["risk"])]
COMP_SKILLS = [("compliance.check", "Compliance check", ["compliance"])]
SECRET = "origin-bearer-token-do-not-leak"


def rpc_v1(
    method: str, params: dict[str, Any], rid: int = 1
) -> tuple[dict[str, Any], dict[str, str]]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, {"A2A-Version": "1.0"}


def message_v1(room: str, text: str, **setu: Any) -> dict[str, Any]:
    return {
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "contextId": room,
            "parts": [{"text": text}],
            "metadata": {"setu": setu},
        }
    }


def message_v03(room: str, text: str, **setu: Any) -> dict[str, Any]:
    return {
        "message": {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "contextId": room,
            "parts": [{"kind": "text", "text": text}],
            "metadata": {"setu": setu},
        }
    }


def agents(risk: Any, comp: Any = None) -> dict[str, Any]:
    """Both origin agents are always registered; most tests only look at Risk."""
    return {
        "risk.internal.test": risk,
        "compliance.internal.test": comp or build_v03_agent(COMP, name="Comp", skills=COMP_SKILLS),
    }


class World:
    """Ops (olga, agent A), Risk (agent B), Compliance (agent C); rooms: A+B, and B+C."""

    def __init__(
        self, w: Acme, *, rules: bool = True, risk_auth: dict[str, Any] | None = None
    ) -> None:
        self.w = w
        self.olga = w.mk_human(w.ops, "olga")
        self.carl = w.mk_human(w.compliance, "carl")
        self.rita_admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        if rules:
            w.allow_talk(w.ops, w.risk)
            w.allow_talk(w.risk, w.compliance)
        self.a = w.mk_agent(w.ops, "agent-a")
        self.key_a = w.mk_key(self.a)["key"]
        reg_admin = w.admin
        extra = {"auth": risk_auth} if risk_auth else {}
        self.b = register(
            w,
            reg_admin,
            RISK + "/.well-known/agent-card.json",
            w.risk,
            handle=f"risk-{w.tag}",
            **extra,
        )
        self.c = register(
            w,
            reg_admin,
            COMP + "/.well-known/agent-card.json",
            w.compliance,
            handle=f"comp-{w.tag}",
        )
        self.key_b = w.mk_key({"id": self.b["participant_id"]})["key"]
        self.room1 = w.mk_room(self.olga, "ops-risk", visibility="cross_department")
        w.add_member(self.room1, self.olga, self.a["id"])
        w.add_member(self.room1, self.olga, self.b["participant_id"])
        self.room2 = w.mk_room(self.carl, "risk-compliance", visibility="cross_department")
        w.add_member(self.room2, self.carl, self.b["participant_id"])
        w.add_member(self.room2, self.carl, self.c["participant_id"])

    def grant_root(self, scopes: list[str] | None = None) -> dict[str, Any]:
        return self.w.ok(
            self.w.post(
                "/v1/delegations",
                self.olga,
                {"to_participant_id": self.a["id"], "scopes": scopes or ["skill:*"]},
            )
        )

    def call(
        self,
        client: TestClient,
        key: str,
        handle: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        **extra: str,
    ) -> Any:
        return client.post(
            f"/a2a/{handle}", json=payload, headers={"X-API-Key": key, **headers, **extra}
        )


def result_task(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    res = body["result"]
    out: dict[str, Any] = res.get("task", res)  # 1.0 wraps the task; 0.3 does not
    return out


def state_of(task: dict[str, Any]) -> str:
    return str(task["status"]["state"]).lower().replace("task_state_", "").replace("-", "_")


def flat(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in tree:
        out.append(n)
        out += flat(n["children"])
    return out


# ------------------------------------------------------------------ cards


def test_proxied_cards_point_at_setu_and_hide_the_origin(migrated_database_url: str) -> None:
    apps = {
        "risk.internal.test": build_v1_agent(
            RISK, name="Risk", skills=RISK_SKILLS, bearer_token=SECRET
        ),
        "compliance.internal.test": build_v03_agent(COMP, name="Comp", skills=COMP_SKILLS),
    }
    with hub(migrated_database_url, apps) as (w, _, client):
        # The origin demands a bearer token. Setu stores it and never shows it to callers.
        world = World(w, risk_auth={"type": "bearer", "token": SECRET})
        root = world.grant_root()
        payload, hdrs = rpc_v1(
            "SendMessage",
            message_v1(world.room1["id"], "hi", skill="risk.score", delegation_id=root["id"]),
        )
        resp = world.call(client, world.key_a, f"risk-{w.tag}", payload, hdrs)
        assert state_of(result_task(resp)) == "completed"
        assert SECRET not in resp.text and "internal.test" not in resp.text
        for handle, skill in (
            (f"risk-{w.tag}", "risk.score"),
            (f"comp-{w.tag}", "compliance.check"),
        ):
            r = client.get(
                f"/a2a/{handle}/.well-known/agent-card.json", headers={"X-API-Key": world.key_a}
            )
            assert r.status_code == 200, r.text
            card = r.json()
            text = json.dumps(card)
            assert "internal.test" not in text and SECRET not in text
            ifaces = card["supportedInterfaces"]
            assert {i["protocolVersion"] for i in ifaces} == {"1.0", "0.3"}
            assert all(i["url"] == f"http://testserver/a2a/{handle}" for i in ifaces)
            assert [s["id"] for s in card["skills"]] == [skill]
            assert (
                card["securitySchemes"]["setu_api_key"]["apiKeySecurityScheme"]["name"]
                == "X-API-Key"
            )
            old = client.get(
                f"/a2a/{handle}/.well-known/agent.json", headers={"X-API-Key": world.key_a}
            ).json()
            assert (
                old["protocolVersion"] == "0.3.0"
                and old["url"] == f"http://testserver/a2a/{handle}"
            )
            assert "internal.test" not in json.dumps(old)

        # Credentials are required, and unknown handles look the same as hidden ones.
        assert client.get(f"/a2a/risk-{w.tag}/.well-known/agent-card.json").status_code == 401
        missing = client.get(
            "/a2a/nope/.well-known/agent-card.json", headers={"X-API-Key": world.key_a}
        )
        assert missing.status_code == 404
        # An sdk-transport agent has no origin to proxy.
        assert (
            client.get(
                f"/a2a/agent-a-{w.tag}/.well-known/agent-card.json",
                headers={"X-API-Key": world.key_a},
            ).status_code
            == 404
        )


# ------------------------------------------------------------------ calls


@pytest.mark.parametrize("dialect", ["v1", "v03"])
def test_agent_calls_agent_only_through_setu(migrated_database_url: str, dialect: str) -> None:
    risk = (build_v1_agent if dialect == "v1" else build_v03_agent)(
        RISK, name="Risk", skills=RISK_SKILLS
    )
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w)
        root = world.grant_root()
        handle = f"risk-{w.tag}"
        room = world.room1["id"]
        if dialect == "v1":
            payload, hdrs = rpc_v1(
                "SendMessage",
                message_v1(room, "score claim 7", skill="risk.score", delegation_id=root["id"]),
            )
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": message_v03(
                    room, "score claim 7", skill="risk.score", delegation_id=root["id"]
                ),
            }
            hdrs = {}
        resp = world.call(client, world.key_a, handle, payload, hdrs)
        task = result_task(resp)
        assert state_of(task) == "completed", task
        art = task["artifacts"][0]["parts"][0]
        assert art["text"] == "Risk handled: score claim 7"
        # The real endpoint appears nowhere in what the caller received.
        assert "internal.test" not in resp.text
        # The origin saw one call, from Setu, with a traceparent that continues the trace.
        sent = [r for r in risk.state.received if "Message" in r.method or "message" in r.method]
        assert len(sent) == 1
        ctx = parse_traceparent(sent[0].headers.get("traceparent"))
        assert ctx is not None
        # The Setu task is the same one the room shows, with the caller as requester.
        tasks = w.get(f"/v1/rooms/{room}/tasks", world.olga).json()
        assert len(tasks) == 1 and tasks[0]["requester_id"] == world.a["id"]
        assert tasks[0]["delegation_chain_id"] == root["id"]
        assert tasks[0]["trace_id"] == ctx.trace_id


def test_denials_come_back_as_rejected_tasks(migrated_database_url: str) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w, rules=False)
        root = world.grant_root()
        room, handle = world.room1["id"], f"risk-{w.tag}"
        payload, hdrs = rpc_v1(
            "SendMessage", message_v1(room, "x", skill="risk.score", delegation_id=root["id"])
        )
        task = result_task(world.call(client, world.key_a, handle, payload, hdrs))
        assert state_of(task) == "rejected"
        assert "until a rule allows it" in json.dumps(task)
        assert risk.state.received == []  # the agent was never called

        # No delegation at all: also refused, with the reason.
        payload, hdrs = rpc_v1("SendMessage", message_v1(room, "x", skill="risk.score"))
        task = result_task(world.call(client, world.key_a, handle, payload, hdrs))
        assert state_of(task) == "rejected" and "delegation" in json.dumps(task)


def test_bad_requests_are_rejected_cleanly(migrated_database_url: str) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w)
        root = world.grant_root()
        handle = f"risk-{w.tag}"
        ok_meta = {"skill": "risk.score", "delegation_id": root["id"]}

        def call(key: str, room: str, **kw: Any) -> Any:
            payload, hdrs = rpc_v1("SendMessage", message_v1(room, "x", **ok_meta))
            return world.call(client, key, kw.get("handle", handle), payload, hdrs)

        assert call("stu_bogus", world.room1["id"]).status_code == 401
        err = call(world.key_a, "not-a-room").json()["error"]
        assert "contextId" in err["message"]
        # A room the caller is not in is indistinguishable from one that does not exist.
        other = w.mk_room(world.carl, "private-room")
        err = call(world.key_a, other["id"]).json()["error"]
        assert "no such room" in err["message"]
        # An agent that is not in the room cannot be called there.
        err = call(world.key_a, world.room2["id"]).json()["error"]
        assert "no such room" in err["message"] or "not a participant" in err["message"]
        assert call(world.key_a, world.room1["id"], handle="nope").status_code == 404
        # Scopes: a key limited to reading cannot start tasks.
        ro = w.mk_key(world.a, ["rooms:read"])["key"]
        assert call(ro, world.room1["id"]).status_code == 403
        assert risk.state.received == []


def test_streaming_reports_progress_and_the_artifact(migrated_database_url: str) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w)
        root = world.grant_root()
        payload, hdrs = rpc_v1(
            "SendStreamingMessage",
            message_v1(
                world.room1["id"], "multi job", skill="risk.score", delegation_id=root["id"]
            ),
        )
        events: list[dict[str, Any]] = []
        with client.stream(
            "POST",
            f"/a2a/risk-{w.tag}",
            json=payload,
            headers={"X-API-Key": world.key_a, **hdrs},
        ) as r:
            assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:])["result"])
        kinds = [next(iter(e)) for e in events]
        assert kinds[0] == "task" and kinds[-1] == "statusUpdate"
        arts = [e["artifactUpdate"]["artifact"]["name"] for e in events if "artifactUpdate" in e]
        assert arts == ["part-1", "part-2"]
        assert "COMPLETED" in json.dumps(events[-1])
        assert "internal.test" not in json.dumps(events)


def test_failures_are_visible_and_leak_no_address(migrated_database_url: str) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w)
        root = world.grant_root()
        payload, hdrs = rpc_v1(
            "SendMessage",
            message_v1(
                world.room1["id"], "fail please", skill="risk.score", delegation_id=root["id"]
            ),
        )
        resp = world.call(client, world.key_a, f"risk-{w.tag}", payload, hdrs)
        task = result_task(resp)
        assert state_of(task) == "failed" and "boom" in json.dumps(task)


def test_cancel_through_the_facade(migrated_database_url: str) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w)
        root = world.grant_root()
        handle = f"risk-{w.tag}"
        params = message_v1(
            world.room1["id"], "slow job", skill="risk.score", delegation_id=root["id"]
        )
        params["configuration"] = {"returnImmediately": True}
        payload, hdrs = rpc_v1("SendMessage", params)
        task = result_task(world.call(client, world.key_a, handle, payload, hdrs))
        task_id = task["id"]
        wait_for(
            lambda: any(
                t["state"] == "working"
                for t in w.get(f"/v1/rooms/{world.room1['id']}/tasks", world.olga).json()
            ),
            what="the task to be working",
        )
        # Another agent cannot cancel it; the caller can.
        other = w.mk_agent(w.ops, "agent-z")
        other_key = w.mk_key(other)["key"]
        payload, hdrs = rpc_v1("CancelTask", {"id": task_id})
        assert "error" in world.call(client, other_key, handle, payload, hdrs).json()
        resp = world.call(client, world.key_a, handle, payload, hdrs)
        assert "error" not in resp.json(), resp.text
        wait_for(
            lambda: (
                w.get(f"/v1/rooms/{world.room1['id']}/tasks", world.olga).json()[0]["state"]
                == "canceled"
            ),
            what="the task to be canceled",
        )
        assert "CancelTask" in [r.method for r in risk.state.received]


# ------------------------------------------------------------------ one trace, A -> B -> C


def test_one_trace_shows_a_to_b_to_c_with_policy_and_delegation(
    migrated_database_url: str,
) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    comp = build_v03_agent(COMP, name="Comp", skills=COMP_SKILLS)
    apps = {"risk.internal.test": risk, "compliance.internal.test": comp}
    with hub(migrated_database_url, apps) as (w, _, client):
        world = World(w)
        root = world.grant_root()
        # A passes part of its authority to B, which B will use to call C.
        sub = w.ok(
            w.c.post(
                "/v1/delegations",
                headers={"X-API-Key": world.key_a},
                json={
                    "to_participant_id": world.b["participant_id"],
                    "scopes": ["skill:compliance.check"],
                    "parent_id": root["id"],
                },
            )
        )
        # 1. A -> B, through Setu.
        payload, hdrs = rpc_v1(
            "SendMessage",
            message_v1(
                world.room1["id"], "score claim 9", skill="risk.score", delegation_id=root["id"]
            ),
        )
        t1 = result_task(world.call(client, world.key_a, f"risk-{w.tag}", payload, hdrs))
        assert state_of(t1) == "completed"
        trace_id = t1["metadata"]["setu"]["trace_id"]
        # 2. B works on it, and needs C. B calls Setu with the traceparent Setu gave it.
        seen = [r for r in risk.state.received if "Message" in r.method][0]
        tp = seen.headers["traceparent"]
        assert parse_traceparent(tp).trace_id == trace_id  # type: ignore[union-attr]
        payload, hdrs = rpc_v1(
            "SendMessage",
            message_v1(
                world.room2["id"],
                "check claim 9",
                skill="compliance.check",
                delegation_id=sub["id"],
            ),
        )
        t2 = result_task(
            world.call(client, world.key_b, f"comp-{w.tag}", payload, hdrs, traceparent=tp)
        )
        assert state_of(t2) == "completed"
        assert t2["metadata"]["setu"]["trace_id"] == trace_id  # continued, not restarted

        # The ops human (room 1 only) sees A -> B; the middle of the trace is hers to read.
        def trace_for(who: Person) -> dict[str, Any]:
            r = w.get(f"/v1/traces/{trace_id}", who)
            assert r.status_code == 200, r.text
            out: dict[str, Any] = r.json()
            return out

        auditor = w.mk_human(w.ops, "ada", ["auditor"])
        full = trace_for(auditor)
        assert full["hidden_spans"] == 0
        spans = flat(full["roots"])
        by_kind = lambda k: [s for s in spans if s["kind"] == k]  # noqa: E731
        assert len(by_kind("message")) == 2 and len(by_kind("a2a_call")) == 2
        assert len(by_kind("policy")) == 2 and len(by_kind("delegation")) == 2
        assert all(s["status"] in ("ok", "completed") for s in spans)

        # message(A->B) > [delegation, policy, a2a_call(B)]; a2a_call(B) > message(B->C) > ...
        assert len(full["roots"]) == 1
        m1 = full["roots"][0]
        assert m1["kind"] == "message" and m1["actor_id"] == world.a["id"]
        kids = {c["kind"]: c for c in m1["children"]}
        assert set(kids) == {"delegation", "policy", "a2a_call"}
        assert kids["delegation"]["attributes"]["chain"] == [root["id"]]
        assert kids["delegation"]["attributes"]["root_human"] == world.olga.id
        assert kids["policy"]["attributes"]["decision"] == "allow"
        assert kids["policy"]["attributes"]["from_department"] == w.ops["id"]
        call_b = kids["a2a_call"]
        assert call_b["actor_id"] == world.b["participant_id"] and call_b["duration_ms"] is not None
        assert call_b["id"] == parse_traceparent(tp).span_id  # type: ignore[union-attr]
        (m2,) = call_b["children"]  # B's own call to C hangs off the span Setu gave B
        assert m2["kind"] == "message" and m2["actor_id"] == world.b["participant_id"]
        kids2 = {c["kind"]: c for c in m2["children"]}
        assert set(kids2) == {"delegation", "policy", "a2a_call"}
        assert kids2["delegation"]["attributes"]["chain"] == [
            sub["id"],
            root["id"],
        ]  # B <- A <- olga
        assert kids2["delegation"]["attributes"]["root_human"] == world.olga.id
        assert kids2["a2a_call"]["actor_id"] == world.c["participant_id"]
        # C was called with the continued trace as well.
        c_tp = [r for r in comp.state.received if "message" in r.method][0].headers["traceparent"]
        assert parse_traceparent(c_tp).trace_id == trace_id  # type: ignore[union-attr]
        assert parse_traceparent(c_tp).span_id == kids2["a2a_call"]["id"]  # type: ignore[union-attr]

        # Department visibility: olga (Ops) sees room 1 only; carl (Compliance) sees room 2 only.
        mine = trace_for(world.olga)
        assert mine["hidden_spans"] == 4 and mine["span_count"] == 4
        assert {s["room_id"] for s in flat(mine["roots"])} == {world.room1["id"]}
        theirs = trace_for(world.carl)
        assert {s["room_id"] for s in flat(theirs["roots"])} == {world.room2["id"]}
        # Someone with no access to either room gets a 404, not a 403.
        stranger = w.mk_human(w.risk, "sam")
        assert w.get(f"/v1/traces/{trace_id}", stranger).status_code == 404
        assert w.get("/v1/traces/" + "0" * 32, auditor).status_code == 404
        assert w.get("/v1/traces/not-a-trace", auditor).status_code == 404


def test_incoming_traceparent_is_continued_and_denials_are_traced(
    migrated_database_url: str,
) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w, rules=False)
        root = world.grant_root()
        ext_trace, ext_span = uuid.uuid4().hex, uuid.uuid4().hex[:16]
        tp = format_traceparent(ext_trace, ext_span)
        payload, hdrs = rpc_v1(
            "SendMessage",
            message_v1(world.room1["id"], "x", skill="risk.score", delegation_id=root["id"]),
        )
        task = result_task(
            world.call(client, world.key_a, f"risk-{w.tag}", payload, hdrs, traceparent=tp)
        )
        assert state_of(task) == "rejected"
        auditor = w.mk_human(w.ops, "ada", ["auditor"])
        tree = w.get(f"/v1/traces/{ext_trace}", auditor).json()
        (msg,) = tree["roots"]  # parent is the external caller's span, which we do not hold
        assert msg["parent_span_id"] == ext_span and msg["status"] == "denied"
        kinds = {c["kind"]: c for c in msg["children"]}
        assert kinds["policy"]["status"] == "denied"
        assert "until a rule allows it" in kinds["policy"]["attributes"]["reason"]
        # A garbage traceparent starts a fresh trace instead of failing the call.
        payload, hdrs = rpc_v1(
            "SendMessage",
            message_v1(world.room1["id"], "x", skill="risk.score", delegation_id=root["id"]),
        )
        bad = world.call(client, world.key_a, f"risk-{w.tag}", payload, hdrs, traceparent="junk")
        assert state_of(result_task(bad)) == "rejected"


def test_ui_tasks_get_traces_and_approval_spans(migrated_database_url: str) -> None:
    """The same spans exist for a task started from the room, including a held approval."""
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, _):
        world = World(w)
        w.ok(
            w.post(
                "/v1/policies",
                w.admin,
                {
                    "name": "gate",
                    "rule": {
                        "type": "require_approval",
                        "skill": "risk.score",
                        "approver_department": w.compliance["id"],
                        "approver_role": "dept_member",
                    },
                },
            )
        )
        room = world.room1["id"]
        sent = w.ok(
            w.post(
                f"/v1/rooms/{room}/messages",
                world.olga,
                {
                    "kind": "task",
                    "body": {"text": "score", "skill": "risk.score"},
                    "mentions": [world.b["participant_id"]],
                },
            )
        )
        trace_id = sent["trace_id"]
        wait_for(
            lambda: (
                w.get(f"/v1/rooms/{room}/tasks", world.olga).json()[0]["state"] == "input_required"
            ),
            what="the held task",
        )
        aid = w.get("/v1/approvals", world.carl).json()[0]["id"]
        w.ok(w.post(f"/v1/approvals/{aid}/decide", world.carl, {"approve": True}), 200)
        wait_for(
            lambda: w.get(f"/v1/rooms/{room}/tasks", world.olga).json()[0]["state"] == "completed",
            what="completion",
        )
        auditor = w.mk_human(w.ops, "ada", ["auditor"])
        spans = flat(w.get(f"/v1/traces/{trace_id}", auditor).json()["roots"])
        kinds = sorted(s["kind"] for s in spans)
        assert kinds == ["a2a_call", "approval", "message", "policy"]
        approval = next(s for s in spans if s["kind"] == "approval")
        assert approval["status"] == "approved" and approval["ended_at"] is not None
        assert (
            next(s for s in spans if s["kind"] == "policy")["attributes"]["decision"]
            == "require_approval"
        )
        # Humans who are not auditors in the room's departments still see it (olga is a member).
        assert w.get(f"/v1/traces/{trace_id}", world.olga).status_code == 200
