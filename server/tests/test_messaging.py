"""Phase 1 acceptance, now authenticated: ordered exchange, and restart loses nothing."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import Acme, Person


class Duo:
    def __init__(self, client: TestClient, url: str) -> None:
        self.w = Acme(client, url)
        self.alice = self.w.mk_human(self.w.ops, "alice")
        self.bob = self.w.mk_human(self.w.ops, "bob")
        self.room = self.w.mk_room(self.alice, "r", visibility="department")  # both in ops

    def ws(self, who: Person, after_seq: int = 0, client: TestClient | None = None) -> Any:
        c = client or self.w.c
        return c.websocket_connect(
            f"/v1/ws/rooms/{self.room['id']}?token={who.token}&after_seq={after_seq}"
        )


def _next(ws: Any) -> dict[str, Any]:
    frame = ws.receive_json()
    assert frame["type"] == "message", frame
    msg: dict[str, Any] = frame["message"]
    return msg


def test_two_clients_exchange_ordered_messages(migrated_database_url: str) -> None:
    with TestClient(create_app(migrated_database_url)) as client:
        d = Duo(client, migrated_database_url)
        with d.ws(d.alice) as a, d.ws(d.bob) as b:
            a.send_json({"type": "send", "body": {"text": "hi bob"}})
            b.send_json({"type": "send", "body": {"text": "hi alice"}})
            b.send_json({"type": "send", "body": {"text": "how are you"}})
            seen_a = [_next(a) for _ in range(3)]
            seen_b = [_next(b) for _ in range(3)]
        assert [m["seq"] for m in seen_a] == [1, 2, 3]
        assert [m["id"] for m in seen_a] == [m["id"] for m in seen_b]
        stored = d.w.get(f"/v1/rooms/{d.room['id']}/messages", d.alice).json()
        assert [m["id"] for m in stored] == [m["id"] for m in seen_a]
        bobs = [m["body"]["text"] for m in seen_a if m["sender_id"] == d.bob.id]
        assert bobs == ["hi alice", "how are you"]


def test_concurrent_senders_get_gapless_seq(migrated_database_url: str) -> None:
    with TestClient(create_app(migrated_database_url)) as client:
        d = Duo(client, migrated_database_url)
        with ThreadPoolExecutor(8) as pool:
            codes = list(
                pool.map(
                    lambda i: d.w.say(d.room, d.alice if i % 2 else d.bob, f"m{i}").status_code,
                    range(40),
                )
            )
        assert set(codes) == {201}
        rows = d.w.get(f"/v1/rooms/{d.room['id']}/messages?limit=100", d.alice).json()
        assert [m["seq"] for m in rows] == list(range(1, 41))


def test_restart_loses_nothing_and_client_catches_up(migrated_database_url: str) -> None:
    with TestClient(create_app(migrated_database_url)) as first:
        d = Duo(first, migrated_database_url)
        for i in range(5):
            d.w.say(d.room, d.alice, f"before-{i}")
        with d.ws(d.bob) as ws:
            assert [_next(ws)["seq"] for _ in range(5)] == [1, 2, 3, 4, 5]
    # "restart": the app and all its connections are gone. Tokens stay valid because the
    # signing secret comes from config, not process memory.
    with TestClient(create_app(migrated_database_url)) as second:
        r = second.post(
            f"/v1/rooms/{d.room['id']}/messages",
            headers=d.alice.headers,
            json={"body": {"text": "after"}},
        )
        assert r.json()["seq"] == 6  # seq continues, not reset
        with d.ws(d.bob, after_seq=5, client=second) as ws:
            msg = _next(ws)
        assert (msg["seq"], msg["body"]) == (6, {"text": "after"})
        rows = second.get(
            f"/v1/rooms/{d.room['id']}/messages?limit=100", headers=d.bob.headers
        ).json()
        assert len(rows) == 6


def test_live_message_arrives_via_notify_after_replay(migrated_database_url: str) -> None:
    with TestClient(create_app(migrated_database_url)) as client:
        d = Duo(client, migrated_database_url)
        d.w.say(d.room, d.alice, "old")
        with d.ws(d.bob, after_seq=1) as ws:
            d.w.say(d.room, d.alice, "live")
            assert _next(ws)["seq"] == 2


def test_reply_to_must_be_in_same_room(migrated_database_url: str) -> None:
    with TestClient(create_app(migrated_database_url)) as client:
        d = Duo(client, migrated_database_url)
        other = d.w.mk_room(d.alice, "other")
        foreign = d.w.say(other, d.alice, "elsewhere").json()
        r = d.w.post(
            f"/v1/rooms/{d.room['id']}/messages",
            d.alice,
            {"body": {"text": "x"}, "reply_to": foreign["id"]},
        )
        assert r.status_code == 422
        own = d.w.say(d.room, d.alice, "parent").json()
        ok = d.w.post(
            f"/v1/rooms/{d.room['id']}/messages",
            d.alice,
            {"body": {"text": "child"}, "reply_to": own["id"]},
        )
        assert ok.status_code == 201


def test_unknown_room_is_404(migrated_database_url: str) -> None:
    import uuid

    with TestClient(create_app(migrated_database_url)) as client:
        d = Duo(client, migrated_database_url)
        r = d.w.post(f"/v1/rooms/{uuid.uuid4()}/messages", d.alice, {"body": {}})
        assert r.status_code == 404


@pytest.mark.parametrize("path", ["/healthz"])
def test_healthz_needs_no_auth(migrated_database_url: str, path: str) -> None:
    with TestClient(create_app(migrated_database_url)) as client:
        assert client.get(path).status_code == 200
