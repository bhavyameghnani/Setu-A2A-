"""Phase 2 acceptance: department visibility, login, roles and API keys."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from tests.helpers import Acme


@pytest.fixture
def world(migrated_database_url: str) -> Any:
    with TestClient(create_app(migrated_database_url)) as client:
        yield Acme(client, migrated_database_url)


def test_login_and_me(world: Acme) -> None:
    ana = world.mk_human(world.ops, "ana")
    me = world.ok(world.get("/v1/auth/me", ana), 200)
    assert me["participant"]["id"] == ana.id
    assert me["roles"] == ["dept_member"]
    bad = world.c.post(
        "/v1/auth/login", json={"email": f"ana-{world.tag}@acme.test", "password": "nope-nope-nope"}
    )
    assert bad.status_code == 401
    assert (
        world.c.post(
            "/v1/auth/login", json={"email": "ghost@acme.test", "password": "x"}
        ).status_code
        == 401
    )


def test_requests_without_valid_credentials_are_rejected(world: Acme) -> None:
    assert world.get("/v1/rooms").status_code == 401
    assert world.c.get("/v1/rooms", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert world.c.get("/v1/rooms", headers={"X-API-Key": "stu_deadbeef_nope"}).status_code == 401


def test_ops_user_cannot_read_a_risk_only_room(world: Acme) -> None:
    ops_user = world.mk_human(world.ops, "olga")
    risk_user = world.mk_human(world.risk, "rita")
    room = world.mk_room(risk_user, "risk-only", visibility="department")
    rid = room["id"]

    # Risk sees it; Ops gets 404 everywhere (not 403: existence is not leaked).
    assert world.get(f"/v1/rooms/{rid}", risk_user).status_code == 200
    assert world.get(f"/v1/rooms/{rid}", ops_user).status_code == 404
    assert world.get(f"/v1/rooms/{rid}/messages", ops_user).status_code == 404
    assert world.say(room, ops_user, "let me in").status_code == 404
    assert rid not in [r["id"] for r in world.get("/v1/rooms", ops_user).json()]
    assert rid in [r["id"] for r in world.get("/v1/rooms", risk_user).json()]
    with pytest.raises(WebSocketDisconnect) as exc:
        with world.c.websocket_connect(f"/v1/ws/rooms/{rid}?token={ops_user.token}"):
            pass
    assert exc.value.code == 4404

    # Even granting access is impossible for a non-manager.
    r = world.c.put(
        f"/v1/rooms/{rid}/access",
        headers=ops_user.headers,
        json={"department_id": world.ops["id"], "access": "read"},
    )
    assert r.status_code == 404


def test_cross_department_room_shows_only_to_granted_departments(world: Acme) -> None:
    ops_user = world.mk_human(world.ops, "olga")
    risk_user = world.mk_human(world.risk, "rita")
    comp_user = world.mk_human(world.compliance, "carl")
    room = world.mk_room(ops_user, "claims-123", visibility="cross_department")
    rid = room["id"]
    world.ok(world.say(room, ops_user, "kickoff"))

    # Nobody else yet.
    for u in (risk_user, comp_user):
        assert world.get(f"/v1/rooms/{rid}", u).status_code == 404

    # Grant Risk read-only: visible, readable, not postable.
    grant = lambda dept, access: world.c.put(  # noqa: E731
        f"/v1/rooms/{rid}/access",
        headers=ops_user.headers,
        json={"department_id": dept["id"], "access": access},
    )
    assert grant(world.risk, "read").status_code == 200
    assert [
        m["body"]["text"] for m in world.get(f"/v1/rooms/{rid}/messages", risk_user).json()
    ] == ["kickoff"]
    assert world.say(room, risk_user, "hello").status_code == 403
    # Compliance still locked out.
    assert world.get(f"/v1/rooms/{rid}", comp_user).status_code == 404

    # Upgrade Risk to participate: now it can post. Compliance still can't see it.
    assert grant(world.risk, "participate").status_code == 200
    assert world.say(room, risk_user, "hello").status_code == 201
    assert world.get(f"/v1/rooms/{rid}", comp_user).status_code == 404

    # Revoke: Risk is locked out again.
    r = world.c.delete(f"/v1/rooms/{rid}/access/{world.risk['id']}", headers=ops_user.headers)
    assert r.status_code == 204
    assert world.get(f"/v1/rooms/{rid}", risk_user).status_code == 404


def test_private_and_department_rooms_reject_cross_department_grants(world: Acme) -> None:
    olga = world.mk_human(world.ops, "olga")
    private = world.mk_room(olga, "p", visibility="private")
    dept_room = world.mk_room(olga, "d", visibility="department")
    for room in (private, dept_room):
        r = world.c.put(
            f"/v1/rooms/{room['id']}/access",
            headers=olga.headers,
            json={"department_id": world.risk["id"], "access": "read"},
        )
        assert r.status_code == 422
    # A private room is not visible to same-department colleagues either, unless they are members.
    colleague = world.mk_human(world.ops, "oscar")
    assert world.get(f"/v1/rooms/{private['id']}", colleague).status_code == 404
    assert world.get(f"/v1/rooms/{dept_room['id']}", colleague).status_code == 200


def test_roles_gate_administration(world: Acme) -> None:
    member = world.mk_human(world.ops, "mia")
    assert world.post("/v1/departments", member, {"name": "X", "slug": "x"}).status_code == 403
    assert (
        world.post(
            "/v1/participants",
            member,
            {"department_id": world.ops["id"], "display_name": "a", "handle": "a1"},
        ).status_code
        == 403
    )
    ops_admin = world.mk_human(world.ops, "adam", ["dept_admin"])
    agent = world.mk_agent(world.ops, "triage")
    assert world.post(f"/v1/participants/{agent['id']}/api-keys", ops_admin, {}).status_code == 201
    # A dept_admin cannot act in another department, nor mint org_admins.
    assert (
        world.post(
            "/v1/participants",
            ops_admin,
            {"department_id": world.risk["id"], "display_name": "a", "handle": "a2"},
        ).status_code
        == 403
    )
    r = world.post(
        "/v1/humans",
        ops_admin,
        {
            "department_id": world.ops["id"],
            "display_name": "evil",
            "handle": "evil",
            "email": "evil@acme.test",
            "password": "long-enough-pw",
            "roles": ["org_admin"],
        },
    )
    assert r.status_code == 403


def test_agent_api_key_flow_scopes_and_revocation(world: Acme) -> None:
    olga = world.mk_human(world.ops, "olga")
    agent = world.mk_agent(world.compliance, "policy-bot")
    key = world.mk_key(agent)
    assert key["key"].startswith("stu_") and "key_hash" not in key
    room = world.mk_room(olga, "cross", visibility="cross_department")

    h = {"X-API-Key": key["key"]}
    # Not a member yet: the agent cannot see the room (departments don't apply to agents).
    assert world.c.get(f"/v1/rooms/{room['id']}", headers=h).status_code == 404
    world.add_member(room, olga, agent["id"])
    assert world.c.get(f"/v1/rooms/{room['id']}", headers=h).status_code == 200
    posted = world.c.post(
        f"/v1/rooms/{room['id']}/messages", headers=h, json={"body": {"text": "beep"}}
    )
    assert posted.status_code == 201 and posted.json()["sender_id"] == agent["id"]
    # Bearer form works too, and the key is listed without secrets.
    assert (
        world.c.get("/v1/rooms", headers={"Authorization": f"Bearer {key['key']}"}).status_code
        == 200
    )
    listed = world.get(f"/v1/participants/{agent['id']}/api-keys", world.admin).json()
    assert listed[0]["last_used_at"] is not None and "key" not in listed[0]

    # Scope enforcement: a read-only key cannot post or create rooms.
    ro = world.mk_key(agent, ["rooms:read"])
    roh = {"X-API-Key": ro["key"]}
    assert world.c.get(f"/v1/rooms/{room['id']}/messages", headers=roh).status_code == 200
    assert (
        world.c.post(f"/v1/rooms/{room['id']}/messages", headers=roh, json={"body": {}}).status_code
        == 403
    )
    assert world.c.post("/v1/rooms", headers=roh, json={"name": "x"}).status_code == 403

    # Revocation takes effect immediately.
    assert (
        world.c.delete(f"/v1/api-keys/{key['id']}", headers=world.admin.headers).status_code == 204
    )
    assert world.c.get(f"/v1/rooms/{room['id']}", headers=h).status_code == 401


def test_websocket_requires_valid_credentials(world: Acme) -> None:
    olga = world.mk_human(world.ops, "olga")
    room = world.mk_room(olga, "ws")
    agent = world.mk_agent(world.ops, "bot")
    key = world.mk_key(agent)
    world.add_member(room, olga, agent["id"])
    url = f"/v1/ws/rooms/{room['id']}"

    with pytest.raises(WebSocketDisconnect) as exc:
        with world.c.websocket_connect(url):
            pass
    assert exc.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as exc:
        with world.c.websocket_connect(f"{url}?token=garbage"):
            pass
    assert exc.value.code == 4401

    # Human via query token, agent via header and via query api_key; each posts as itself.
    with world.c.websocket_connect(f"{url}?token={olga.token}") as ws:
        ws.send_json({"type": "send", "body": {"text": "from olga"}})
        assert ws.receive_json()["message"]["sender_id"] == olga.id
    with world.c.websocket_connect(url, headers={"X-API-Key": key["key"]}) as ws:
        ws.send_json({"type": "send", "body": {"text": "from bot"}})
        seen = [ws.receive_json()["message"] for _ in range(2)]
    assert [m["sender_id"] for m in seen] == [olga.id, agent["id"]]
    with world.c.websocket_connect(f"{url}?api_key={key['key']}&after_seq=2") as ws:
        ws.send_json({"type": "send", "kind": "bogus", "body": {}})
        assert ws.receive_json()["type"] == "error"


def test_websocket_read_only_member_cannot_send(world: Acme) -> None:
    olga = world.mk_human(world.ops, "olga")
    watcher = world.mk_human(world.risk, "wes")
    room = world.mk_room(olga, "w", visibility="cross_department")
    world.add_member(room, olga, watcher.id, "observer")
    with world.c.websocket_connect(f"/v1/ws/rooms/{room['id']}?token={watcher.token}") as ws:
        ws.send_json({"type": "send", "body": {"text": "nope"}})
        assert ws.receive_json() == {"type": "error", "detail": "you cannot post to this room"}
    assert world.get(f"/v1/rooms/{room['id']}/messages", olga).json() == []


def test_cross_org_participants_cannot_be_added(world: Acme, migrated_database_url: str) -> None:
    other = Acme(world.c, migrated_database_url)  # a second, separate org
    olga = world.mk_human(world.ops, "olga")
    room = world.mk_room(olga, "r")
    foreign = other.mk_human(other.ops, "frank")
    r = world.post(f"/v1/rooms/{room['id']}/members", olga, {"participant_id": foreign.id})
    assert r.status_code == 404
    assert world.get(f"/v1/rooms/{room['id']}", foreign).status_code == 404
