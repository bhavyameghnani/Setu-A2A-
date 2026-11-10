"""Phase 3 acceptance: unaddressed agents do not act; restarted agents resume from cursor."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.snapshots import run_snapshots_once
from app.main import create_app
from tests.helpers import Acme


class Cast:
    """An Ops human plus two agents (triage in Ops, scorer in Risk) in one cross-department room."""

    def __init__(self, client: TestClient, url: str) -> None:
        self.w = Acme(client, url)
        self.w.allow_talk(self.w.ops, "*")  # cross-department traffic is denied by default
        self.olga = self.w.mk_human(self.w.ops, "olga")
        self.scorer = self.w.mk_agent(self.w.risk, "scorer")
        self.auditor_bot = self.w.mk_agent(self.w.compliance, "checker")
        self.scorer_key = self.w.mk_key(self.scorer)["key"]
        self.checker_key = self.w.mk_key(self.auditor_bot)["key"]
        self.room = self.w.mk_room(self.olga, "claims", visibility="cross_department")
        self.w.add_member(self.room, self.olga, self.scorer["id"])
        self.w.add_member(self.room, self.olga, self.auditor_bot["id"])

    @property
    def rid(self) -> str:
        return str(self.room["id"])

    def agent_headers(self, key: str) -> dict[str, str]:
        return {"X-API-Key": key}

    def say(self, text: str, **extra: Any) -> dict[str, Any]:
        r = self.w.post(
            f"/v1/rooms/{self.rid}/messages", self.olga, {"body": {"text": text}, **extra}
        )
        assert r.status_code == 201, r.text
        out: dict[str, Any] = r.json()
        return out


class ToyAgent:
    """Test-only agent loop: acts only when addressed, then acks (real one: Phase 8 SDK)."""

    def __init__(self, client: TestClient, cast: Cast, me: dict[str, Any], key: str) -> None:
        self.c, self.cast, self.me, self.key = client, cast, me, key
        self.acted_on: list[int] = []

    def run_until(self, seen_seq: int) -> None:
        """Handle frames by type (replies interleave with live messages) until the server has
        confirmed my cursor reached `seen_seq`."""
        url = f"/v1/ws/rooms/{self.cast.rid}"
        with self.c.websocket_connect(url, headers={"X-API-Key": self.key}) as ws:
            while True:
                frame = ws.receive_json()
                if frame["type"] == "message":
                    msg = frame["message"]
                    if self.me["id"] in msg["addressed"]:  # the only-act-when-addressed guard
                        self.acted_on.append(msg["seq"])
                        ws.send_json(
                            {"type": "status", "message_id": msg["id"], "status": "completed"}
                        )
                    ws.send_json({"type": "ack", "seq": msg["seq"]})
                elif frame["type"] == "cursor" and frame["last_seq"] >= seen_seq:
                    return
                else:
                    assert frame["type"] in ("cursor", "status"), frame


@pytest.fixture
def cast(migrated_database_url: str) -> Any:
    with TestClient(create_app(migrated_database_url)) as client:
        yield Cast(client, migrated_database_url), client


def test_unaddressed_agents_do_not_act(cast: Any) -> None:
    c, client = cast
    c.say("score this please", mentions=[c.scorer["id"]])
    c.say("just chatting, nobody mentioned")
    m3 = c.say("compliance only", mentions=[c.auditor_bot["id"]])

    scorer = ToyAgent(client, c, c.scorer, c.scorer_key)
    checker = ToyAgent(client, c, c.auditor_bot, c.checker_key)
    scorer.run_until(3)
    checker.run_until(3)
    assert scorer.acted_on == [1]
    assert checker.acted_on == [3]
    assert m3["addressed"] == [c.auditor_bot["id"]]

    # Server-side record agrees: only addressed participants have a status row to work on.
    stored = {
        m["seq"]: m["addressed"] for m in c.w.get(f"/v1/rooms/{c.rid}/messages", c.olga).json()
    }
    assert stored == {1: [c.scorer["id"]], 2: [], 3: [c.auditor_bot["id"]]}
    # Not addressed => cannot claim to have handled it.
    msg2 = c.w.get(f"/v1/rooms/{c.rid}/messages", c.olga).json()[1]
    r = client.put(
        f"/v1/rooms/{c.rid}/messages/{msg2['id']}/status",
        headers=c.agent_headers(c.scorer_key),
        json={"status": "completed"},
    )
    assert r.status_code == 404


def test_restarted_agent_catches_up_from_its_cursor(cast: Any) -> None:
    c, client = cast
    for i in range(1, 4):
        c.say(f"job {i}", mentions=[c.scorer["id"]])
    scorer = ToyAgent(client, c, c.scorer, c.scorer_key)
    scorer.run_until(3)
    assert scorer.acted_on == [1, 2, 3]
    cur = client.get(f"/v1/rooms/{c.rid}/cursor", headers=c.agent_headers(c.scorer_key)).json()
    assert cur["last_seq"] == 3

    # The agent is down while more work arrives (some for it, some not).
    c.say("job 4", mentions=[c.scorer["id"]])
    c.say("chatter")
    c.say("job 6", mentions=[c.scorer["id"]])

    # Restarted process: fresh object, no memory, no after_seq. It resumes from the server cursor
    # and sees neither 1-3 again nor misses 4-6.
    reborn = ToyAgent(client, c, c.scorer, c.scorer_key)
    reborn.run_until(6)
    assert reborn.acted_on == [4, 6]
    assert (
        client.get(f"/v1/rooms/{c.rid}/cursor", headers=c.agent_headers(c.scorer_key)).json()[
            "last_seq"
        ]
        == 6
    )


def test_inbox_lists_unfinished_work_after_crash(cast: Any) -> None:
    c, client = cast
    ids = [c.say(f"job {i}", mentions=[c.scorer["id"]])["id"] for i in range(3)]
    h = c.agent_headers(c.scorer_key)
    put = lambda mid, st: client.put(  # noqa: E731
        f"/v1/rooms/{c.rid}/messages/{mid}/status", headers=h, json={"status": st}
    )
    assert put(ids[0], "acked").status_code == 200
    assert put(ids[0], "completed").status_code == 200
    assert put(ids[1], "acked").status_code == 200  # crashed mid-work
    inbox = client.get(f"/v1/rooms/{c.rid}/inbox", headers=h).json()
    assert [m["id"] for m in inbox] == [ids[1], ids[2]]
    # Lifecycle rules: terminal states are final, retries are harmless.
    assert put(ids[0], "completed").status_code == 200
    assert put(ids[0], "failed").status_code == 409
    assert put(ids[0], "acked").status_code == 409
    # The human who asked has nothing pending: they were never addressed.
    assert client.get(f"/v1/rooms/{c.rid}/inbox", headers=c.olga.headers).json() == []


def test_cursor_is_monotonic_and_bounded(cast: Any) -> None:
    c, client = cast
    for i in range(3):
        c.say(f"m{i}")
    h = c.olga.headers
    put = lambda n: client.put(f"/v1/rooms/{c.rid}/cursor", headers=h, json={"last_seq": n})  # noqa: E731
    assert put(2).json()["last_seq"] == 2
    assert put(1).json()["last_seq"] == 2  # a late duplicate does not rewind
    assert put(3).json()["last_seq"] == 3
    assert put(4).status_code == 422  # beyond the end
    # Another participant's cursor is independent.
    other = client.get(f"/v1/rooms/{c.rid}/cursor", headers=c.agent_headers(c.scorer_key)).json()
    assert other["last_seq"] == 0


def test_explicit_after_seq_overrides_cursor(cast: Any) -> None:
    c, client = cast
    for i in range(4):
        c.say(f"m{i}")
    client.put(f"/v1/rooms/{c.rid}/cursor", headers=c.olga.headers, json={"last_seq": 3})
    base = f"/v1/ws/rooms/{c.rid}?token={c.olga.token}"
    with client.websocket_connect(base) as ws:
        assert ws.receive_json()["message"]["seq"] == 4
    with client.websocket_connect(base + "&after_seq=0") as ws:
        assert ws.receive_json()["message"]["seq"] == 1


def test_subscriptions_address_agents_by_kind_and_skill(cast: Any) -> None:
    c, client = cast
    h = c.agent_headers(c.scorer_key)
    sub = client.put(
        f"/v1/rooms/{c.rid}/subscriptions", headers=h, json={"kind": "task", "skill": "risk.score"}
    )
    assert sub.status_code == 200 and sub.json()["skill"] == "risk.score"
    again = client.put(
        f"/v1/rooms/{c.rid}/subscriptions", headers=h, json={"kind": "task", "skill": "risk.score"}
    )
    assert again.json()["id"] == sub.json()["id"]  # idempotent

    def send(kind: str, body: dict[str, Any]) -> dict[str, Any]:
        r = c.w.post(f"/v1/rooms/{c.rid}/messages", c.olga, {"kind": kind, "body": body})
        out: dict[str, Any] = r.json()
        return out

    hit = send("task", {"skill": "risk.score", "claim": 123})
    miss_skill = send("task", {"skill": "tax.file"})
    miss_kind = send("text", {"skill": "risk.score"})
    multi = send("task", {"skills": ["x", "risk.score"]})
    assert hit["addressed"] == [c.scorer["id"]]
    assert miss_skill["addressed"] == [] and miss_kind["addressed"] == []
    assert multi["addressed"] == [c.scorer["id"]]

    assert [s["id"] for s in client.get(f"/v1/rooms/{c.rid}/subscriptions", headers=h).json()] == [
        sub.json()["id"]
    ]
    assert (
        client.delete(f"/v1/rooms/{c.rid}/subscriptions/{sub.json()['id']}", headers=h).status_code
        == 204
    )
    assert send("task", {"skill": "risk.score"})["addressed"] == []
    # Observers and outsiders cannot subscribe.
    assert (
        client.put(f"/v1/rooms/{c.rid}/subscriptions", headers=c.olga.headers, json={}).status_code
        == 200
    )
    watcher = c.w.mk_human(c.w.compliance, "wes")
    c.w.add_member(c.room, c.olga, watcher.id, "observer")
    assert (
        client.put(f"/v1/rooms/{c.rid}/subscriptions", headers=watcher.headers, json={}).status_code
        == 403
    )
    stranger = c.w.mk_human(c.w.risk, "sam")
    assert (
        client.put(
            f"/v1/rooms/{c.rid}/subscriptions", headers=stranger.headers, json={}
        ).status_code
        == 404
    )


def test_activity_messages_and_non_members_address_nobody(cast: Any) -> None:
    c, _ = cast
    stranger = c.w.mk_human(c.w.risk, "sam")
    r = c.w.post(
        f"/v1/rooms/{c.rid}/messages",
        c.olga,
        {"kind": "activity", "body": {"text": "fyi"}, "mentions": [c.scorer["id"], stranger.id]},
    )
    assert r.json()["addressed"] == []
    r = c.w.post(f"/v1/rooms/{c.rid}/messages", c.olga, {"body": {}, "mentions": [stranger.id]})
    assert r.json()["addressed"] == []


def test_snapshot_job_summarizes_new_messages_once(migrated_database_url: str) -> None:
    import asyncio

    from app.db.session import make_engine, make_sessionmaker

    with TestClient(create_app(migrated_database_url)) as client:
        c = Cast(client, migrated_database_url)
        for i in range(6):
            c.say(f"m{i}")

        async def job(min_messages: int) -> int:
            eng = make_engine(migrated_database_url)
            try:
                return await run_snapshots_once(make_sessionmaker(eng), min_messages)
            finally:
                await eng.dispose()

        # Other tests' rooms share this database and may qualify; ours has only 6 messages.
        asyncio.run(job(10))
        assert client.get(f"/v1/rooms/{c.rid}/snapshots", headers=c.olga.headers).json() == []
        assert asyncio.run(job(5)) >= 1
        snaps = client.get(f"/v1/rooms/{c.rid}/snapshots", headers=c.olga.headers).json()
        assert [s["up_to_seq"] for s in snaps] == [6]
        assert snaps[0]["summary_text"].startswith("Messages 1-6 (6): 6 text.")
        asyncio.run(job(5))  # nothing new: no duplicate snapshot
        assert len(client.get(f"/v1/rooms/{c.rid}/snapshots", headers=c.olga.headers).json()) == 1
        c.say("later")
        for i in range(5):
            c.say(f"n{i}")
        asyncio.run(job(5))
        snaps = client.get(f"/v1/rooms/{c.rid}/snapshots", headers=c.olga.headers).json()
        assert [s["up_to_seq"] for s in snaps] == [12, 6]  # newest first, second covers 7-12
        assert snaps[0]["summary_text"].startswith("Messages 7-12")
        outsider = c.w.mk_human(c.w.risk, "sam")
        assert (
            client.get(f"/v1/rooms/{c.rid}/snapshots", headers=outsider.headers).status_code == 404
        )
