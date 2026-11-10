"""Phase 7 acceptance: in the cross-department demo, humans in Ops, Risk and Compliance each
receive the right notifications for their department; an approval from a Compliance user
resumes the task."""

import time
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.a2a_agents import build_v1_agent
from tests.helpers import Acme, Person
from tests.test_a2a_flow import RISK, SKILLS, hub, register, wait_for


class Demo:
    """Ops (olga, otto), Risk (rita, rick + the Risk agent), Compliance (carl admin, cora member).

    Cross-department traffic is allowed Ops -> Risk, and risk.score needs a Compliance admin."""

    def __init__(self, w: Acme, *, gate: bool = True, allow: bool = True) -> None:
        self.w = w
        self.olga = w.mk_human(w.ops, "olga")
        self.otto = w.mk_human(w.ops, "otto")
        self.rita = w.mk_human(w.risk, "rita")
        self.rick = w.mk_human(w.risk, "rick")
        self.carl = w.mk_human(w.compliance, "carl", ["dept_admin"])
        self.cora = w.mk_human(w.compliance, "cora")
        self.everyone = (self.olga, self.otto, self.rita, self.rick, self.carl, self.cora)
        self.risk_admin = w.mk_human(w.risk, "ruth", ["dept_admin"])
        self.agent = register(
            w,
            self.risk_admin,
            RISK + "/.well-known/agent-card.json",
            w.risk,
            handle=f"risk-{w.tag}",
        )
        self.pid = self.agent["participant_id"]
        if allow:
            w.allow_talk(w.ops, w.risk)
        if gate:
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
                            "approver_role": "dept_admin",
                        },
                    },
                )
            )
        self.room = w.mk_room(self.olga, "claims-123", visibility="cross_department")
        w.add_member(self.room, self.olga, self.pid)
        self.rid = self.room["id"]

    def send(self, text: str = "score claim 123", **extra: Any) -> Any:
        return self.w.post(
            f"/v1/rooms/{self.rid}/messages",
            self.olga,
            {
                "kind": "task",
                "body": {"text": text, "skill": "risk.score"},
                "mentions": [self.pid],
                **extra,
            },
        )

    def inbox(self, who: Person, **q: Any) -> dict[str, Any]:
        r = self.w.get("/v1/notifications", who, params={"limit": 100, **q})
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    def events(self, who: Person) -> list[str]:
        """Event types, oldest first, so a list reads like the story."""
        return [n["event_type"] for n in reversed(self.inbox(who)["items"])]

    def task(self, n: int = 0) -> dict[str, Any]:
        ts = sorted(
            self.w.get(f"/v1/rooms/{self.rid}/tasks", self.olga).json(),
            key=lambda t: t["created_at"],
        )
        return ts[n]

    def wait_state(self, state: str, n: int = 0) -> None:
        wait_for(lambda: self.task(n)["state"] == state, what=f"task to be {state}")


def test_cross_department_demo_notifies_each_department(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        # Everyone is created before the agent is registered, so the Risk team hears about it.
        d = Demo(w)
        assert [d.events(p) for p in (d.rita, d.rick, d.risk_admin)] == [["agent.registered"]] * 3
        assert d.events(d.olga) == d.events(d.carl) == []  # other departments are not told
        reg = d.inbox(d.rita)["items"][0]
        assert reg["summary"] == "Risk-Agent was registered for Risk."

        # Olga (Ops) asks the Risk agent; policy says Compliance must sign off first.
        sent = w.ok(d.send())
        d.wait_state("input_required")
        time.sleep(0.3)
        assert app.state.received == []  # held: the agent has not been called

        asked = "approval.requested"
        # Ops (requester) and Risk (assignee) are told; so is the Compliance *admin* who can act.
        assert d.events(d.olga) == d.events(d.otto) == [asked]
        assert d.events(d.rita) == d.events(d.rick) == ["agent.registered", asked]
        assert d.events(d.carl) == [asked]
        assert d.events(d.cora) == []  # a Compliance member without the admin role cannot decide
        note = d.inbox(d.carl)["items"][0]
        assert note["summary"].startswith(
            "Approval needed from Compliance: olga asks Risk-Agent for risk.score"
        )
        assert note["task_id"] == d.task()["id"] and note["trace_id"] == sent["trace_id"]
        assert note["actions"]["approval_id"] is not None  # carl can decide it from the inbox
        assert note["actions"]["open_room"] is False  # no access to the Ops room, by design
        assert d.inbox(d.olga)["items"][0]["actions"]["approval_id"] is None  # not her own request
        assert d.inbox(d.olga)["items"][0]["actions"]["cancel_task"] is True
        assert d.inbox(d.rita)["items"][0]["actions"] == {
            "open_room": False, "reply": False, "approval_id": None, "cancel_task": False,
        }  # fmt: skip

        # A Compliance user approves from the inbox: the task resumes and finishes.
        approval_id = note["actions"]["approval_id"]
        w.ok(w.post(f"/v1/approvals/{approval_id}/decide", d.carl, {"approve": True}), 200)
        d.wait_state("completed")
        wait_for(lambda: "task.completed" in d.events(d.carl), what="completion notice")

        tail = ["approval.granted", "task.started", "task.completed"]
        assert d.events(d.olga) == d.events(d.otto) == [asked, *tail]
        assert d.events(d.rita) == d.events(d.rick) == ["agent.registered", asked, *tail]
        assert d.events(d.cora) == tail  # the approving department hears the outcome
        assert d.events(d.carl) == [asked, "task.started", "task.completed"]  # not her own decision
        done = d.inbox(d.olga)["items"][0]
        assert done["summary"] == "Risk-Agent finished risk.score for olga. Result attached."
        # The approval is no longer actionable once decided.
        assert d.inbox(d.carl)["items"][-1]["actions"]["approval_id"] is None
        # Olga can open the room from it and reply.
        assert done["actions"]["open_room"] and done["actions"]["reply"]


def test_inbox_unread_counts_paging_and_privacy(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        d = Demo(w, gate=False)
        for i in range(3):
            w.ok(d.send(f"score {i}"))
            d.wait_state("completed", i)
        wait_for(lambda: d.inbox(d.olga)["unread"] == 6, what="six notices (3 started, 3 done)")

        page1 = d.inbox(d.olga, limit=4)
        assert page1["unread"] == 6 and len(page1["items"]) == 4 and page1["next_before"]
        page2 = d.inbox(d.olga, limit=4, before=page1["next_before"])
        assert len(page2["items"]) == 2 and page2["next_before"] is None
        ids = [n["id"] for n in page1["items"] + page2["items"]]
        assert ids == sorted(ids, reverse=True) and len(set(ids)) == 6  # newest first, no overlap

        first = page1["items"][0]
        assert w.get("/v1/notifications/unread-count", d.olga).json() == {"unread": 6}
        marked = w.ok(w.post(f"/v1/notifications/{first['id']}/read", d.olga, {}), 200)
        assert marked["read_at"] is not None
        w.ok(w.post(f"/v1/notifications/{first['id']}/read", d.olga, {}), 200)  # idempotent
        assert d.inbox(d.olga)["unread"] == 5
        assert len(d.inbox(d.olga, unread_only=True)["items"]) == 5

        # Nobody else can see, read or reply to it; the other Ops person has their own copies.
        for other in (d.otto, d.rita):
            assert w.get(f"/v1/notifications/{first['id']}", other).status_code == 404
            assert w.post(f"/v1/notifications/{first['id']}/read", other, {}).status_code == 404
            assert (
                w.post(f"/v1/notifications/{first['id']}/reply", other, {"text": "hi"}).status_code
                == 404
            )
        assert d.inbox(d.otto)["unread"] == 6

        assert w.ok(w.post("/v1/notifications/read-all", d.olga, {}), 200) == {"marked": 5}
        assert d.inbox(d.olga)["unread"] == 0 and d.inbox(d.otto)["unread"] == 6

        # Agents have no inbox; unauthenticated callers are refused.
        key = w.mk_key({"id": d.pid})["key"]
        assert w.c.get("/v1/notifications", headers={"X-API-Key": key}).status_code == 403
        assert w.c.get("/v1/notifications").status_code == 401


def test_reply_from_a_notification_reaches_the_agent_thread(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        d = Demo(w, gate=False)
        w.ok(d.send())
        d.wait_state("completed")
        wait_for(lambda: len(d.inbox(d.olga)["items"]) == 2, what="two notices")
        n = d.inbox(d.olga)["items"][0]
        msg = w.ok(
            w.post(f"/v1/notifications/{n['id']}/reply", d.olga, {"text": "thanks, rescore at 9"})
        )
        assert msg["kind"] == "text" and msg["sender_id"] == d.olga.id
        assert (
            msg["task_id"] == n["task_id"] and d.pid in msg["addressed"]
        )  # the agent can act on it
        assert w.get(f"/v1/notifications/{n['id']}", d.olga).json()["read_at"] is not None
        # Rita (Risk) has no access to the Ops room: her notification cannot open or reply there.
        rn = d.inbox(d.rita)["items"][0]
        assert rn["actions"]["open_room"] is False
        assert (
            w.post(f"/v1/notifications/{rn['id']}/reply", d.rita, {"text": "hi"}).status_code == 404
        )
        # Granting her department access to the room changes what the same notice allows.
        w.ok(
            w.c.put(
                f"/v1/rooms/{d.rid}/access",
                headers=d.olga.headers,
                json={"department_id": w.risk["id"], "access": "participate"},
            ),
            200,
        )
        assert d.inbox(d.rita)["items"][0]["actions"]["reply"] is True
        w.ok(w.post(f"/v1/notifications/{rn['id']}/reply", d.rita, {"text": "on it"}))


def test_cancel_a_task_from_its_notification(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        d = Demo(w, gate=False)
        w.ok(d.send("slow job"))
        d.wait_state("working")
        started = d.inbox(d.olga)["items"][0]
        assert started["event_type"] == "task.started" and started["actions"]["cancel_task"] is True
        assert d.inbox(d.rita)["items"][0]["actions"]["cancel_task"] is False
        w.ok(w.post(f"/v1/tasks/{started['task_id']}/cancel", d.olga, {}), 200)
        wait_for(lambda: "task.canceled" in d.events(d.rita), what="cancel notice")
        assert d.events(d.olga)[-1] == "task.canceled"
        assert (
            d.inbox(d.olga)["items"][0]["actions"]["cancel_task"] is False
        )  # nothing left to cancel


def test_failures_and_questions_reach_the_people_involved(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        d = Demo(w, gate=False)
        w.ok(d.send("fail now"))
        d.wait_state("failed")
        wait_for(lambda: "task.failed" in d.events(d.olga), what="failure notice")
        failed = d.inbox(d.olga)["items"][0]
        assert (
            failed["summary"].endswith("failed risk.score for Olga: boom.")
            or "boom" in failed["summary"]
        )
        assert "task.failed" in d.events(d.rita)

        w.ok(d.send("input needed"))
        d.wait_state("input_required", 1)
        wait_for(lambda: "task.input_required" in d.events(d.olga), what="question notice")
        q = d.inbox(d.olga)["items"][0]
        assert "which claim id?" in q["summary"]
        assert q["actions"]["approval_id"] is not None  # she can answer it from the inbox
        w.ok(
            w.post(
                f"/v1/approvals/{q['actions']['approval_id']}/decide",
                d.olga,
                {"approve": True, "note": "42"},
            ),
            200,
        )
        d.wait_state("completed", 1)
        assert d.events(d.olga)[-1] == "task.completed"


def test_policy_denials_notify_both_departments(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        d = Demo(w, gate=False, allow=False)
        assert d.send().status_code == 403
        for who in (d.olga, d.otto, d.rita, d.rick):
            assert d.events(who)[-1] == "policy.denied"
        assert d.events(d.carl) == []  # Compliance is not involved
        n = d.inbox(d.olga)["items"][0]
        assert "until a rule allows it" in n["summary"] and n["task_id"] is None
        assert n["actions"]["open_room"] is True and n["trace_id"]  # links to the denied trace


def test_denied_approval_tells_everyone_involved(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        d = Demo(w)
        w.ok(d.send())
        d.wait_state("input_required")
        aid = d.inbox(d.carl)["items"][0]["actions"]["approval_id"]
        w.ok(w.post(f"/v1/approvals/{aid}/decide", d.carl, {"approve": False, "note": "no"}), 200)
        d.wait_state("rejected")
        assert (
            d.events(d.olga)[-1]
            == d.events(d.rita)[-1]
            == d.events(d.cora)[-1]
            == "approval.denied"
        )
        assert (
            d.events(d.carl)[-1] == "approval.requested"
        )  # the decider is not notified of herself


def test_card_changes_notify_the_owning_department(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        d = Demo(w, gate=False)
        before = len(d.events(d.rita))
        assert (
            w.ok(w.post(f"/v1/agents/{d.pid}/refresh", d.risk_admin, {}), 200)["changed"] is False
        )
        assert len(d.events(d.rita)) == before  # unchanged card: silence
        app.state.skills = [*app.state.skills, ("risk.explain", "Explain", ["risk"])]
        assert w.ok(w.post(f"/v1/agents/{d.pid}/refresh", d.risk_admin, {}), 200)["changed"] is True
        last = d.inbox(d.rita)["items"][0]
        assert last["event_type"] == "agent.card_changed" and "risk.explain" in last["summary"]
        assert d.events(d.olga) == []  # only the owning department


def test_live_push_and_replay(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk-Agent", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, client):
        d = Demo(w, gate=False, allow=False)
        with client.websocket_connect(f"/v1/ws/notifications?token={d.olga.token}") as ws:
            assert ws.receive_json() == {"type": "unread", "unread": 0}
            assert d.send().status_code == 403  # a denial: one notice for Olga
            frame = ws.receive_json()
            assert frame["type"] == "notification" and frame["unread"] == 1
            assert frame["notification"]["event_type"] == "policy.denied"
            first_id = frame["notification"]["id"]
            assert frame["notification"]["actions"]["open_room"] is True
            assert d.send().status_code == 403
            assert ws.receive_json()["unread"] == 2
        # A reconnect with a cursor replays only what came after it.
        with client.websocket_connect(
            f"/v1/ws/notifications?token={d.olga.token}&after_id={first_id}"
        ) as ws:
            assert ws.receive_json()["unread"] == 2
            replayed = ws.receive_json()
            assert replayed["type"] == "notification" and replayed["notification"]["id"] != first_id
        # Without a cursor nothing old is replayed: the first frame after "unread" is live.
        with client.websocket_connect(f"/v1/ws/notifications?token={d.olga.token}") as ws:
            assert ws.receive_json()["unread"] == 2
            assert d.send().status_code == 403
            assert ws.receive_json()["unread"] == 3
        # People only: an agent key, or no credentials, are turned away.
        key = w.mk_key({"id": d.pid})["key"]
        for kw in ({"headers": {"X-API-Key": key}}, {}):
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/v1/ws/notifications", **kw) as ws:
                    ws.receive_json()
            assert exc.value.code in (4401, 4403)
