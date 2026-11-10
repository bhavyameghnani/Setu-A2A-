"""Phase 5 acceptance: cross-department calls are denied until a rule allows them; a
`require_approval` rule pauses the task until a human approves; sub-delegation cannot exceed
scope; tampering with the audit log is detected."""

import asyncio
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.db.session import make_engine
from tests.a2a_agents import build_v1_agent
from tests.helpers import Acme, Person
from tests.test_a2a_flow import BUILDERS, RISK, SKILLS, Scene, hub, wait_for


def audit_events(w: Acme, event_type: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = w.get("/v1/audit?limit=500", w.admin).json()
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def agent_calls(app: Any) -> list[Any]:
    return [r for r in app.state.received if r.method in ("SendStreamingMessage", "message/stream")]


def gate_rule(w: Acme, skill: str = "risk.score", role: str = "dept_admin") -> dict[str, Any]:
    return w.ok(
        w.post(
            "/v1/policies",
            w.admin,
            {
                "name": "risk scoring needs compliance sign-off",
                "rule": {
                    "type": "require_approval",
                    "skill": skill,
                    "approver_department": w.compliance["id"],
                    "approver_role": role,
                },
            },
        )
    )


# ------------------------------------------------------------------ 1. default deny


def test_cross_department_is_denied_until_a_rule_allows_it(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w, allow=False)
        denied = w.post(
            f"/v1/rooms/{s.rid}/messages",
            s.olga,
            {"kind": "task", "body": {"text": "score", "skill": "risk.score"}, "mentions": [s.pid]},
        )
        assert denied.status_code == 403 and "until a rule allows it" in denied.json()["detail"]
        assert s.messages() == [] and s.tasks() == [] and agent_calls(app) == []
        ev = audit_events(w, "policy.deny")
        assert len(ev) == 1 and ev[0]["actor_id"] == s.olga.id
        assert ev[0]["payload"]["recipient_department"] == w.risk["id"]

        rule = w.allow_talk(w.ops, w.risk)
        s.send_task("score claim 1")
        s.task_when("completed")
        assert audit_events(w, "policy.allow")[0]["payload"]["rule_id"] == rule["id"]

        # Rules can be switched off again; the very next send is denied.
        off = w.c.put(
            f"/v1/policies/{rule['id']}", headers=w.admin.headers, json={"enabled": False}
        )
        assert off.status_code == 200
        again = w.post(
            f"/v1/rooms/{s.rid}/messages",
            s.olga,
            {"kind": "task", "body": {"text": "again"}, "mentions": [s.pid]},
        )
        assert again.status_code == 403

        # Only org admins edit policies; members cannot read the audit log.
        assert w.get("/v1/audit", s.olga).status_code == 403
        assert w.get("/v1/policies", s.olga).status_code == 403
        bad = w.post("/v1/policies", w.admin, {"name": "x", "rule": {"type": "allow_talk"}})
        assert bad.status_code == 422
        assert w.post("/v1/policies", s.olga, {"name": "x", "rule": {}}).status_code == 403


def test_text_messages_are_policed_too_but_replies_are_not(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w, allow=False)
        r = w.post(
            f"/v1/rooms/{s.rid}/messages",
            s.olga,
            {"kind": "text", "body": {"text": "hey"}, "mentions": [s.pid]},
        )
        assert r.status_code == 403
        # Unaddressed context messages are never policed.
        assert w.say(s.room, s.olga, "just talking").status_code == 201


# ------------------------------------------------------------------ 2. approvals


def test_require_approval_pauses_the_task_until_a_human_approves(
    migrated_database_url: str,
) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        gate = gate_rule(w)
        carl = w.mk_human(w.compliance, "carl", ["dept_admin"])
        cora = w.mk_human(w.compliance, "cora")  # a Compliance member, but not an admin

        s.send_task("score claim 123")
        task = s.task_when("input_required")
        time.sleep(0.4)
        assert agent_calls(app) == []  # held: the agent has not been called
        assert task["completed_at"] is None

        req = [m for m in s.messages() if m["kind"] == "approval_request"]
        assert len(req) == 1 and "Approval required" in req[0]["body"]["prompt"]
        assert audit_events(w, "policy.require_approval")[0]["payload"]["rule_id"] == gate["id"]
        assert audit_events(w, "approval.requested")

        pending = w.get("/v1/approvals", carl).json()
        assert len(pending) == 1 and pending[0]["kind"] == "policy"
        assert pending[0]["task_id"] == task["id"] and pending[0]["state"] == "pending"
        aid = pending[0]["id"]
        assert w.get("/v1/approvals", cora).json() == []  # wrong role: not even listed
        assert w.get("/v1/approvals", s.risk_admin).json() == []  # wrong department

        # The requester, another department and a member without the role cannot decide.
        for who, codes in ((s.olga, {403}), (s.risk_admin, {404}), (cora, {404})):
            r = w.post(f"/v1/approvals/{aid}/decide", who, {"approve": True})
            assert r.status_code in codes, (who.id, r.status_code, r.text)
        assert s.tasks()[0]["state"] == "input_required"

        done = w.ok(
            w.post(f"/v1/approvals/{aid}/decide", carl, {"approve": True, "note": "ok"}), 200
        )
        assert done["state"] == "approved" and done["decided_by"] == carl.id
        s.task_when("completed")
        assert len(agent_calls(app)) == 1
        kinds = [m["kind"] for m in s.messages()]
        assert "approval_response" in kinds
        assert audit_events(w, "approval.granted")
        # Deciding twice is a conflict, not a second release.
        assert w.post(f"/v1/approvals/{aid}/decide", carl, {"approve": True}).status_code == 409


def test_denied_approval_rejects_the_task(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        gate_rule(w)
        carl = w.mk_human(w.compliance, "carl", ["dept_admin"])
        s.send_task("score claim 9")
        s.task_when("input_required")
        aid = w.get("/v1/approvals", carl).json()[0]["id"]
        w.ok(w.post(f"/v1/approvals/{aid}/decide", carl, {"approve": False, "note": "no"}), 200)
        task = s.task_when("rejected")
        assert task["completed_at"]
        time.sleep(0.3)
        assert agent_calls(app) == []
        upd = [m for m in s.messages() if m["kind"] == "task_update"]
        assert upd[-1]["body"]["state"] == "rejected"
        assert audit_events(w, "approval.denied")
        # Org admins can decide anything in their org.
        s.send_task("score claim 10")
        s.task_when("input_required", 1)
        aid2 = w.get("/v1/approvals", w.admin).json()[0]["id"]
        assert w.post(f"/v1/approvals/{aid2}/decide", w.admin, {"approve": True}).status_code == 200
        s.task_when("completed", 1)


def test_rule_added_after_send_is_enforced_at_call_time(migrated_database_url: str) -> None:
    """Policy is evaluated on every A2A call, not only when the message is sent."""
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w, allow=False)
        # Task creation needs an allow rule; revoke it before the dispatcher picks the task up.
        s.w.allow_talk(w.ops, w.risk)
        rules = w.get("/v1/policies", w.admin).json()
        from app.a2a import dispatch

        gate_calls: list[int] = []
        orig = dispatch.Dispatcher._claim

        async def slow_claim(self: Any) -> list[uuid.UUID]:
            if not gate_calls:  # first poll: still let the rule change land before dispatch
                gate_calls.append(1)
                await asyncio.sleep(0.5)
            return await orig(self)

        dispatch.Dispatcher._claim = slow_claim  # type: ignore[method-assign]
        try:
            s.send_task("score")
            w.c.put(
                f"/v1/policies/{rules[0]['id']}", headers=w.admin.headers, json={"enabled": False}
            )
            t = s.task_when("rejected")
        finally:
            dispatch.Dispatcher._claim = orig  # type: ignore[method-assign]
        assert t["error"] and agent_calls(app) == []


@pytest.mark.parametrize("flavor", ["v1", "v03"])
def test_agent_question_is_answered_by_the_requester_and_resumes(
    migrated_database_url: str, flavor: str
) -> None:
    app = BUILDERS[flavor](RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        s.send_task("input needed")
        s.task_when("input_required")
        pending = w.get("/v1/approvals", s.olga).json()
        assert len(pending) == 1 and pending[0]["kind"] == "agent"
        assert pending[0]["summary"] == "which claim id?"
        aid = pending[0]["id"]
        other = w.mk_human(w.ops, "otto")  # same department, but not the requester
        assert w.post(f"/v1/approvals/{aid}/decide", other, {"approve": True}).status_code == 404
        w.ok(
            w.post(f"/v1/approvals/{aid}/decide", s.olga, {"approve": True, "note": "claim 42"}),
            200,
        )
        s.task_when("completed")
        sent = agent_calls(app)
        assert len(sent) == 2
        msg = sent[1].body["params"]["message"]
        assert msg["taskId"]  # same A2A task, not a new one
        texts = [p.get("text") for p in msg["parts"]]
        assert "claim 42" in texts


def test_denying_an_agent_question_cancels_the_task(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        s.send_task("input needed")
        s.task_when("input_required")
        aid = w.get("/v1/approvals", s.olga).json()[0]["id"]
        w.ok(w.post(f"/v1/approvals/{aid}/decide", s.olga, {"approve": False}), 200)
        s.task_when("canceled")


# ------------------------------------------------------------------ 3. delegation


class Bots:
    """Olga (Ops human) with two agents in a cross-department room with the Risk agent."""

    def __init__(self, w: Acme, s: Scene) -> None:
        self.w, self.s = w, s
        self.a = w.mk_agent(w.ops, "agent-a")
        self.b = w.mk_agent(w.ops, "agent-b")
        self.key_a = w.mk_key(self.a)["key"]
        self.key_b = w.mk_key(self.b)["key"]
        w.add_member(s.room, s.olga, self.a["id"])
        w.add_member(s.room, s.olga, self.b["id"])

    def grant(
        self, who: Person, to: dict[str, Any], scopes: list[str], **extra: Any
    ) -> dict[str, Any]:
        return self.w.ok(
            self.w.post(
                "/v1/delegations", who, {"to_participant_id": to["id"], "scopes": scopes, **extra}
            )
        )

    def agent_grant(
        self, key: str, to: dict[str, Any], scopes: list[str], parent: str, **extra: Any
    ) -> Any:
        return self.w.c.post(
            "/v1/delegations",
            headers={"X-API-Key": key},
            json={"to_participant_id": to["id"], "scopes": scopes, "parent_id": parent, **extra},
        )

    def send(self, key: str, **extra: Any) -> Any:
        return self.w.c.post(
            f"/v1/rooms/{self.s.rid}/messages",
            headers={"X-API-Key": key},
            json={
                "kind": "task",
                "body": {"text": "score", "skill": "risk.score"},
                "mentions": [self.s.pid],
                **extra,
            },
        )


def test_sub_delegation_cannot_exceed_scope_or_lifetime(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        bots = Bots(w, s)
        in_1h = "2099-01-01T01:00:00Z"
        in_2h = "2099-01-01T02:00:00Z"
        root = bots.grant(s.olga, bots.a, ["skill:risk.score"], expires_at=in_1h)

        # Wider scope, a different skill, a later expiry or no expiry: all refused.
        for scopes, extra, why in (
            (["skill:*"], {"expires_at": in_1h}, "scopes exceed"),
            (["skill:other"], {"expires_at": in_1h}, "scopes exceed"),
            (["skill:risk.score"], {"expires_at": in_2h}, "outlive"),
            (["skill:risk.score"], {}, "outlive"),
        ):
            r = bots.agent_grant(bots.key_a, bots.b, scopes, root["id"], **extra)
            assert r.status_code == 403 and why in r.json()["detail"], (scopes, extra, r.text)
        # An agent cannot mint authority out of nothing, nor use someone else's delegation.
        no_parent = bots.w.c.post(
            "/v1/delegations",
            headers={"X-API-Key": bots.key_a},
            json={"to_participant_id": bots.b["id"], "scopes": ["skill:risk.score"]},
        )
        assert no_parent.status_code == 403 and "must name" in no_parent.json()["detail"]
        theirs = bots.agent_grant(
            bots.key_b, bots.a, ["skill:risk.score"], root["id"], expires_at=in_1h
        )
        assert theirs.status_code == 403 and "not granted to the requester" in theirs.text

        ok = bots.agent_grant(
            bots.key_a, bots.b, ["skill:risk.score"], root["id"], expires_at=in_1h
        )
        assert ok.status_code == 201, ok.text
        leaf = ok.json()
        assert leaf["root_human_id"] == s.olga.id and leaf["parent_id"] == root["id"]

        chain = w.get(f"/v1/delegations/{leaf['id']}/chain", s.olga).json()
        assert [d["id"] for d in chain] == [leaf["id"], root["id"]]  # leaf first, root last
        assert chain[-1]["from_participant_id"] == s.olga.id  # the root human
        assert w.get(f"/v1/delegations/{leaf['id']}/chain", s.risk_admin).status_code == 404
        assert w.get(f"/v1/delegations/{leaf['id']}/chain", w.admin).status_code == 200
        assert len(audit_events(w, "delegation.created")) == 2


def test_agents_need_a_covering_unrevoked_delegation_to_send_tasks(
    migrated_database_url: str,
) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        bots = Bots(w, s)
        no_deleg = bots.send(bots.key_a)
        assert no_deleg.status_code == 403 and "delegation" in no_deleg.json()["detail"]
        assert audit_events(w, "delegation.denied")

        narrow = bots.grant(s.olga, bots.a, ["skill:other"])
        too_narrow = bots.send(bots.key_a, delegation_id=narrow["id"])
        assert too_narrow.status_code == 403 and "not covered" in too_narrow.json()["detail"]
        not_mine = bots.send(bots.key_b, delegation_id=narrow["id"])
        assert not_mine.status_code == 403 and "not granted" in not_mine.json()["detail"]

        good = bots.grant(s.olga, bots.a, ["skill:risk.score"])
        sent = bots.send(bots.key_a, delegation_id=good["id"])
        assert sent.status_code == 201, sent.text
        assert sent.json()["delegation_chain_id"] == good["id"]
        task = s.task_when("completed")
        assert task["delegation_chain_id"] == good["id"]  # every task carries its chain

        # Revoking ends it: the next send is refused.
        assert w.post(f"/v1/delegations/{good['id']}/revoke", s.olga, {}).status_code == 200
        revoked = bots.send(bots.key_a, delegation_id=good["id"])
        assert revoked.status_code == 403 and "revoked" in revoked.json()["detail"]
        assert audit_events(w, "delegation.revoked")
        # Only the grantor, root human or org admin may revoke.
        other = bots.grant(s.olga, bots.a, ["skill:*"])
        assert w.post(f"/v1/delegations/{other['id']}/revoke", s.risk_admin, {}).status_code == 404


# ------------------------------------------------------------------ 4. audit


def test_audit_is_hash_chained_and_tampering_is_detected(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w, allow=False)
        for _ in range(3):
            w.post(
                f"/v1/rooms/{s.rid}/messages",
                s.olga,
                {"kind": "task", "body": {"text": "x"}, "mentions": [s.pid]},
            )
        w.allow_talk(w.ops, w.risk)
        events = audit_events(w)
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1)) and len(events) >= 4
        assert events[0]["prev_hash"] == "0" * 64
        assert events[1]["prev_hash"] == events[0]["hash"]
        auditor = w.mk_human(w.ops, "ada", ["auditor"])
        verified = w.ok(w.post("/v1/audit/verify", auditor, {}), 200)
        assert verified == {"ok": True, "checked": len(events), "first_broken_seq": None}
        assert w.post("/v1/audit/verify", s.olga, {}).status_code == 403

        org = uuid.UUID(w.admin.data["org_id"])
        outcome = asyncio.run(tamper(migrated_database_url, org, 2))
        assert "append-only" in outcome["blocked"]  # the trigger refuses UPDATE and DELETE
        broken = w.ok(w.post("/v1/audit/verify", auditor, {}), 200)
        assert broken["ok"] is False and broken["first_broken_seq"] == 2


async def tamper(url: str, org: uuid.UUID, seq: int) -> dict[str, str]:
    engine = make_engine(url)
    out = {"blocked": ""}
    try:
        async with engine.begin() as conn:
            for stmt in (
                "update audit_log set event_type = 'x' where org_id = :o and seq = :s",
                "delete from audit_log where org_id = :o and seq = :s",
                "truncate audit_log",
            ):
                try:
                    async with conn.begin_nested():
                        await conn.execute(text(stmt), {"o": org, "s": seq} if ":o" in stmt else {})
                    out["blocked"] += " NOT-BLOCKED:" + stmt
                except Exception as exc:
                    out["blocked"] += str(exc)
        async with engine.begin() as conn:  # the table owner can still bypass it, deliberately
            await conn.execute(text("alter table audit_log disable trigger user"))
            try:
                await conn.execute(
                    text(
                        "update audit_log set payload = '{}'::jsonb where org_id = :o and seq = :s"
                    ),
                    {"o": org, "s": seq},
                )
            finally:
                await conn.execute(text("alter table audit_log enable trigger user"))
    finally:
        await engine.dispose()
    return out


def test_concurrent_events_keep_one_consistent_chain(migrated_database_url: str) -> None:
    with hub(migrated_database_url, {}) as (w, _, _):
        results: list[Any] = []

        def make(i: int) -> Any:
            return w.post(
                "/v1/policies",
                w.admin,
                {
                    "name": f"p{i}",
                    "rule": {"type": "deny_talk", "from_department": "*", "to_department": "*"},
                },
            )

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(make, range(12)))
        assert all(r.status_code == 201 for r in results)
        v = w.ok(w.post("/v1/audit/verify", w.admin, {}), 200)
        assert v["ok"] and v["checked"] == 12


def test_room_membership_and_access_changes_are_audited(migrated_database_url: str) -> None:
    with hub(migrated_database_url, {}) as (w, _, _):
        olga = w.mk_human(w.ops, "olga")
        room = w.mk_room(olga, "r", visibility="cross_department")
        sdk = w.mk_agent(w.risk, "bot")
        w.add_member(room, olga, sdk["id"])
        w.ok(
            w.c.put(
                f"/v1/rooms/{room['id']}/access",
                headers=olga.headers,
                json={"department_id": w.risk["id"], "access": "read"},
            ),
            200,
        )
        assert (
            w.c.delete(
                f"/v1/rooms/{room['id']}/access/{w.risk['id']}", headers=olga.headers
            ).status_code
            == 204
        )
        types = [e["event_type"] for e in audit_events(w)]
        assert types == ["room.member_added", "room.access_granted", "room.access_revoked"]
        assert audit_events(w, "room.member_added")[0]["payload"]["self_added"] is False


def test_wait_helper_is_importable() -> None:
    assert wait_for(lambda: True)  # keeps the shared helper exercised from this module
