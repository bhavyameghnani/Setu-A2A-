"""setu-sdk against a real hub over real sockets: client, Agent, LangGraph adapter, a2a_serve."""

import asyncio
import contextlib
import signal
import uuid
from typing import Any, TypedDict

import asyncpg
import httpx
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from setu_sdk import Agent, Message, SetuClient, SetuError
from setu_sdk.a2a_serve import (
    A2ARequest,
    Skill,
    build_a2a_app,
    free_port,
    graph_handler,
    serve,
)
from setu_sdk.adapters.langgraph import LangGraphAgent, SetuContext
from sqlalchemy.engine import make_url

from tests.conftest import LiveHub
from tests.helpers import Acme
from tests.sdk_support import Proxy, Served, live_acme, running, until


class World:
    """Ops: olga (human), bot and bot2 (SDK agents with API keys), one private room."""

    def __init__(self, hub: LiveHub) -> None:
        self.hub = hub
        self.w: Acme = live_acme(hub.url, hub.database_url)
        self.olga = self.w.mk_human(self.w.ops, "olga")
        self.bot = self.w.mk_agent(self.w.ops, "bot")
        self.bot2 = self.w.mk_agent(self.w.ops, "bot2")
        self.key = self.w.mk_key(self.bot)["key"]
        self.key2 = self.w.mk_key(self.bot2)["key"]
        self.room = self.w.mk_room(self.olga, "work")
        self.rid: str = self.room["id"]
        self.w.add_member(self.room, self.olga, self.bot["id"])
        self.w.add_member(self.room, self.olga, self.bot2["id"])

    def client(self, key: str | None = None, base: str | None = None) -> SetuClient:
        return SetuClient(base or self.hub.url, key or self.key)

    def say(self, text: str, *mention: str, kind: str = "text", **body: Any) -> dict[str, Any]:
        r = self.w.post(
            f"/v1/rooms/{self.rid}/messages",
            self.olga,
            {"kind": kind, "body": {"text": text, **body}, "mentions": list(mention)},
        )
        return self.w.ok(r)

    def messages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = self.w.get(
            f"/v1/rooms/{self.rid}/messages?limit=500", self.olga
        ).json()
        return out

    async def status(self, message_id: str, participant: str) -> str | None:
        u = make_url(self.hub.database_url)
        conn = await asyncpg.connect(user=u.username, host=u.host, port=u.port, database=u.database)
        try:
            row = await conn.fetchrow(
                "select status from message_status where message_id = $1 and participant_id = $2",
                uuid.UUID(message_id),
                uuid.UUID(participant),
            )
        finally:
            await conn.close()
        return None if row is None else str(row["status"])


@pytest.fixture
def world(live_hub: LiveHub) -> World:
    return World(live_hub)


class Recorder(Agent):
    def __init__(self, *a: Any, fail_on: str | None = None, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.acted: list[Message] = []
        self.context: list[Message] = []
        self.fail_on = fail_on

    async def on_message(self, msg: Message) -> None:
        self.acted.append(msg)
        if self.fail_on and self.fail_on in msg.text:
            raise RuntimeError("deliberate")
        await self.reply(msg, {"text": f"got: {msg.text}"})

    async def on_context(self, msg: Message) -> None:
        self.context.append(msg)


# ------------------------------------------------------------------ client


async def test_client_send_replay_cursor_and_inbox(world: World) -> None:
    async with world.client() as c:
        me = await c.whoami()
        assert (
            me["participant"]["id"] == world.bot["id"]
            and await c.participant_id() == world.bot["id"]
        )
        assert [r["id"] for r in await c.rooms()] == [world.rid]
        sent = await c.send(world.rid, {"text": "hello"}, mentions=[world.olga.id])
        assert isinstance(sent, Message) and sent.text == "hello" and sent.seq == 1
        for i in range(2, 6):
            world.say(f"m{i}", world.bot["id"])
        assert [m.seq async for m in c.replay(world.rid)] == [1, 2, 3, 4, 5]
        assert [m.seq async for m in c.replay(world.rid, after_seq=3)] == [4, 5]
        assert await c.cursor(world.rid) == 0
        assert await c.ack(world.rid, 3) == 3
        assert await c.ack(world.rid, 5) == 5
        assert [m.text for m in await c.inbox(world.rid)] == ["m2", "m3", "m4", "m5"]
        await c.set_status(world.rid, (await c.inbox(world.rid))[0].id, "completed")
        assert len(await c.inbox(world.rid)) == 3
        # Errors carry the hub's status and message.
        with pytest.raises(SetuError) as exc:
            await c.send(world.rid, {"text": "x"}, kind="nonsense")
        assert exc.value.status == 422
        with pytest.raises(SetuError) as exc:
            await c.room("00000000-0000-0000-0000-000000000000")
        assert exc.value.status == 404
    with pytest.raises(SetuError) as exc:
        async with SetuClient(world.hub.url, "stu_bogus") as bad:
            await bad.whoami()
    assert exc.value.status == 401


async def test_stream_replays_then_goes_live(world: World) -> None:
    world.say("before", world.bot["id"])
    async with world.client() as c:
        got: list[str] = []

        async def reader() -> None:
            async for m in c.stream(world.rid, after_seq=0):
                got.append(m.text)

        task = asyncio.create_task(reader())
        await until(lambda: got == ["before"], what="replayed message")
        world.say("live")
        await until(lambda: got == ["before", "live"], what="live message")
        task.cancel()


# ------------------------------------------------------------------ Agent


async def test_agent_acts_only_when_addressed_and_acks(world: World) -> None:
    async with world.client() as c:
        agent = Recorder(c, fail_on="boom", discover_every=0.2)
        async with running(agent):
            mine = world.say("for bot", world.bot["id"])
            world.say("for nobody")
            world.say("for bot2", world.bot2["id"])
            bad = world.say("boom", world.bot["id"])
            await until(lambda: len(agent.acted) == 2, what="two addressed messages")
            await until(lambda: len(agent.context) == 2, what="two context messages")
            assert [m.text for m in agent.acted] == ["for bot", "boom"]
            assert [m.text for m in agent.context] == ["for nobody", "for bot2"]
            # acked -> completed (or failed); the cursor reached the end.
            last = world.messages()[-1]["seq"]
            await until(lambda: _cursor_is(c, world.rid, last), what="cursor at end")
            assert await world.status(mine["id"], world.bot["id"]) == "completed"
            assert await world.status(bad["id"], world.bot["id"]) == "failed"
            assert await c.inbox(world.rid) == []
            # It did not act on its own replies, and its replies are in the room.
            replies = [m for m in world.messages() if m["sender_id"] == world.bot["id"]]
            assert [r["body"]["text"] for r in replies] == ["got: for bot"]
            assert replies[0]["reply_to"] == mine["id"] and world.olga.id in replies[0]["addressed"]
            assert len(agent.acted) == 2  # nothing re-ran on the replies


async def _status_is(world: World, message_id: str, who: str, expected: str) -> bool:
    return await world.status(message_id, who) == expected


async def _cursor_is(c: SetuClient, room: str, seq: int) -> bool:
    return await c.cursor(room) == seq


async def test_restarted_agent_catches_up_without_repeating_work(world: World) -> None:
    async with world.client() as c:
        first = Recorder(c, discover_every=0.2)
        async with running(first):
            world.say("one", world.bot["id"])
            await until(lambda: len(first.acted) == 1, what="first message")
            await until(lambda: _cursor_is(c, world.rid, 2), what="cursor")
        # The agent is down while two more arrive.
        world.say("two", world.bot["id"])
        world.say("three", world.bot["id"])
        second = Recorder(c, discover_every=0.2)
        async with running(second):
            await until(lambda: len(second.acted) == 2, what="catch-up")
            assert [m.text for m in second.acted] == ["two", "three"]
            await asyncio.sleep(0.5)
            assert len(second.acted) == 2


async def test_crash_recovery_resumes_unfinished_and_skips_finished(world: World) -> None:
    started = world.say("was mid-flight", world.bot["id"])
    finished = world.say("already done", world.bot["id"])
    later = world.say("new", world.bot["id"])
    async with world.client() as c:
        # What a crash leaves behind: one message acked but never completed, one completed but
        # its cursor never advanced.
        await c.set_status(world.rid, started["id"], "acked")
        await c.set_status(world.rid, finished["id"], "completed")
        agent = Recorder(c, discover_every=0.2)
        async with running(agent):
            await until(lambda: len(agent.acted) == 2, what="resumed work")
            await asyncio.sleep(0.4)
        assert sorted(m.text for m in agent.acted) == ["new", "was mid-flight"]
        assert later["seq"] > finished["seq"]


async def test_agent_reconnects_after_the_connection_drops(world: World) -> None:
    proxy = Proxy(world.hub.port)
    await proxy.start()
    try:
        async with SetuClient(proxy.url, world.key) as c:
            agent = Recorder(c, discover_every=0.2, backoff_initial=0.1, backoff_max=0.5)
            async with running(agent):
                world.say("one", world.bot["id"])
                await until(lambda: len(agent.acted) == 1, what="first")
                proxy.drop()  # every socket (REST keep-alives and the stream) is cut
                world.say("two", world.bot["id"])
                await until(lambda: len(agent.acted) == 2, what="message sent while disconnected")
                world.say("three", world.bot["id"])
                await until(lambda: len(agent.acted) == 3, what="live again")
                assert [m.text for m in agent.acted] == [
                    "one",
                    "two",
                    "three",
                ]  # none lost, none doubled
    finally:
        await proxy.stop()


async def test_agent_joins_rooms_it_is_added_to_later(world: World) -> None:
    async with world.client() as c:
        agent = Recorder(c, discover_every=0.2)
        async with running(agent):
            room2 = world.w.mk_room(world.olga, "second")
            world.w.add_member(room2, world.olga, world.bot["id"])
            world.w.ok(
                world.w.post(
                    f"/v1/rooms/{room2['id']}/messages",
                    world.olga,
                    {"body": {"text": "hi from room 2"}, "mentions": [world.bot["id"]]},
                )
            )
            await until(lambda: len(agent.acted) == 1, what="message in the new room")
            assert agent.acted[0].room_id == room2["id"]


# ------------------------------------------------------------------ LangGraph adapter


class S(TypedDict, total=False):
    text: str
    data: Any
    skill: Any
    message: Any
    output: str
    steps: list[str]


def make_graph(fail: bool = False, who: str | None = None) -> Any:
    """read -> think. With `who` unset the name comes from Setu (the SDK adapter's context)."""

    async def read(state: S) -> S:
        return {"steps": ["read"]}

    async def think(state: S, config: RunnableConfig) -> S:
        if fail:
            raise ValueError("graph blew up")
        name = who
        if name is None:
            setu: SetuContext = config["configurable"]["setu"]
            name = (await setu.client.whoami())["participant"]["display_name"]
        return {"output": f"{name} says: {state['text'].upper()}", "steps": ["think"]}

    g = StateGraph(S)
    g.add_node("read", read)
    g.add_node("think", think)
    g.add_edge(START, "read")
    g.add_edge("read", "think")
    g.add_edge("think", END)
    return g.compile()


async def test_langgraph_agent_runs_the_graph_for_each_addressed_message(world: World) -> None:
    async with world.client() as c:
        agent = LangGraphAgent(c, make_graph(), progress=True, discover_every=0.2)
        async with running(agent):
            req = world.say("score this", world.bot["id"], kind="task", skill="x.y")
            await until(
                lambda: any(m["kind"] == "result" for m in world.messages()), what="a result"
            )
            await until(
                lambda: any(m["body"].get("state") == "completed" for m in world.messages()),
                what="completion",
            )
        msgs = [m for m in world.messages() if m["sender_id"] == world.bot["id"]]
        assert [m["kind"] for m in msgs] == [
            "task_update", "task_update", "task_update", "result", "task_update",
        ]  # fmt: skip
        states = [m["body"]["state"] for m in msgs if m["kind"] == "task_update"]
        assert states == ["working", "working", "working", "completed"]
        assert [m["body"].get("text") for m in msgs[1:3]] == ["step: read", "step: think"]
        result = next(m for m in msgs if m["kind"] == "result")
        assert result["body"]["text"] == "bot says: SCORE THIS"
        assert result["body"]["request_message_id"] == req["id"] and result["reply_to"] == req["id"]
        assert world.olga.id in result["addressed"]


async def test_langgraph_plain_text_gets_a_text_reply_and_failures_are_visible(
    world: World,
) -> None:
    async with world.client() as c:
        async with running(LangGraphAgent(c, make_graph(), discover_every=0.2)):
            world.say("hello there", world.bot["id"])
            await until(
                lambda: any(
                    m["body"].get("text", "").startswith("bot says") for m in world.messages()
                ),
                what="reply",
            )
        reply = [m for m in world.messages() if m["sender_id"] == world.bot["id"]]
        assert [m["kind"] for m in reply] == ["text"] and reply[0]["body"][
            "text"
        ] == "bot says: HELLO THERE"

    async with world.client(world.key2) as c2:
        async with running(LangGraphAgent(c2, make_graph(fail=True), discover_every=0.2)):
            req = world.say("this fails", world.bot2["id"], kind="task")
            await until(
                lambda: any(m["body"].get("state") == "failed" for m in world.messages()),
                what="failure",
            )
            await until(
                lambda: _status_is(world, req["id"], world.bot2["id"], "failed"),
                what="failed delivery status",
            )
        failed = next(m for m in world.messages() if m["body"].get("state") == "failed")
        assert "ValueError: graph blew up" in failed["body"]["error"]


async def test_langgraph_agent_ignores_replies_and_answers_failed_text_requests(
    world: World,
) -> None:
    async with world.client() as c:
        async with running(LangGraphAgent(c, make_graph(fail=True), discover_every=0.2)):
            # Setu addresses an agent with replies to its own tasks; those are not requests.
            world.say("fyi", world.bot["id"], kind="result")
            world.say("fyi", world.bot["id"], kind="task_update", state="working")
            # A plain-text request whose graph fails is answered, not left hanging.
            world.say("please", world.bot["id"])
            await until(
                lambda: any(
                    m["body"].get("text", "").startswith("I could not do that")
                    for m in world.messages()
                ),
                what="the explanation",
            )
        mine = [m for m in world.messages() if m["sender_id"] == world.bot["id"]]
        assert len(mine) == 1  # one answer, to the one request
        assert "ValueError: graph blew up" in mine[0]["body"]["text"]


# ------------------------------------------------------------------ a2a_serve


def register(w: Acme, world: World, card_url: str, handle: str, **kw: Any) -> dict[str, Any]:
    admin = w.mk_human(w.risk, f"adm-{handle}", ["dept_admin"])
    return w.ok(
        w.post(
            "/v1/agents/register",
            admin,
            {
                "card_url": card_url,
                "department_id": w.risk["id"],
                "handle": f"{handle}-{w.tag}",
                **kw,
            },
        )
    )


async def test_a2a_serve_a_callable_registers_and_completes_tasks(world: World) -> None:
    async def scorer(req: A2ARequest) -> dict[str, Any]:
        await req.progress("looking up the claim")
        assert req.setu["task_id"] and "traceparent" in req.headers  # what Setu sends every agent
        return {"claim": req.data[0]["claim"], "risk": "low", "echo": req.text}

    port = free_port()
    app = build_a2a_app(
        scorer,
        name="Scorer",
        url=f"http://127.0.0.1:{port}",
        skills=[Skill("risk.score", "Risk scoring", tags=["risk"])],
    )
    w = world.w
    w.allow_talk(w.ops, w.risk)
    with Served(app, port) as srv:
        async with httpx.AsyncClient() as http:
            card = (await http.get(srv.url + "/.well-known/agent-card.json")).json()
            old = (await http.get(srv.url + "/.well-known/agent.json")).json()
        assert {i["protocolVersion"] for i in card["supportedInterfaces"]} == {"1.0", "0.3"}
        assert card["skills"][0]["id"] == "risk.score"
        assert old["protocolVersion"] == "0.3.0" and old["url"] == srv.url + "/rpc"
        reg = register(w, world, srv.url + "/.well-known/agent-card.json", "scorer")
        assert reg["a2a_version"] == "1.0" and reg["skills"][0]["id"] == "risk.score"
        w.add_member(world.room, world.olga, reg["participant_id"])
        async with world.client() as c:
            # A human-less call: the SDK bot is in Ops, so it needs a delegation to ask Risk.
            root = w.ok(
                w.post(
                    "/v1/delegations",
                    world.olga,
                    {"to_participant_id": world.bot["id"], "scopes": ["skill:risk.score"]},
                )
            )
            msg = await c.send_task(
                world.rid, reg["participant_id"], text="score claim 7", data={"claim": 7},
                skill="risk.score", delegation_id=root["id"],
            )  # fmt: skip
            task = await c.task_for(msg)
            seen = [t["state"] async for t in c.subscribe_task(task["id"], max_wait=20)]
            assert seen[-1] == "completed"
            done = await c.get_task(task["id"])
            part = done["artifacts"][0]["parts"][0]["data"]
            assert part == {"claim": 7, "risk": "low", "echo": "score claim 7"}
            progress = [
                m["body"].get("text") for m in world.messages() if m["kind"] == "task_update"
            ]
            assert "looking up the claim" in progress


async def test_a2a_serve_failures_bearer_auth_and_dialects(world: World) -> None:
    async def boom(req: A2ARequest) -> str:
        raise RuntimeError("the model is down")

    port = free_port()
    app = build_a2a_app(
        boom,
        name="Boom",
        url=f"http://127.0.0.1:{port}",
        skills=[Skill("x", "X")],
        bearer_token="s3cret",
    )
    w = world.w
    w.allow_talk(w.ops, w.risk)
    with Served(app, port) as srv:
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
            "kind": "message", "messageId": "m1", "role": "user",
            "parts": [{"kind": "text", "text": "hi"}]}}}  # fmt: skip
        async with httpx.AsyncClient() as http:
            assert (await http.post(srv.url + "/rpc", json=rpc)).status_code == 401  # enforced
            ok = (
                await http.post(
                    srv.url + "/rpc", json=rpc, headers={"Authorization": "Bearer s3cret"}
                )
            ).json()
        assert (
            ok["result"]["status"]["state"] == "failed"
        )  # a 0.3 caller works on the same endpoint
        assert "the model is down" in str(ok["result"]["status"])

        reg = register(w, world, srv.url, "boom", auth={"type": "bearer", "token": "s3cret"})
        w.add_member(world.room, world.olga, reg["participant_id"])
        sent = w.ok(
            w.post(
                f"/v1/rooms/{world.rid}/messages",
                world.olga,
                {"kind": "task", "body": {"text": "go"}, "mentions": [reg["participant_id"]]},
            )
        )
        async with world.client() as c:
            task = await c.task_for(Message.from_json(sent))
            final = await c.wait_task(task["id"], max_wait=20)
        assert final["state"] == "failed"
        # An agent that reports its own failure explains itself in the room.
        said = [m["body"].get("text", "") for m in world.messages() if m["kind"] == "task_update"]
        assert any("the model is down" in t for t in said)


async def test_a2a_serve_a_langgraph_graph(world: World) -> None:
    port = free_port()
    app = build_a2a_app(
        graph_handler(make_graph(who="Graphy")),
        name="Graphy",
        url=f"http://127.0.0.1:{port}",
        skills=[Skill("graph.say", "Say things")],
    )
    w = world.w
    w.allow_talk(w.ops, w.risk)
    with Served(app, port) as srv:
        reg = register(w, world, srv.url, "graphy")
        w.add_member(world.room, world.olga, reg["participant_id"])
        sent = w.ok(
            w.post(
                f"/v1/rooms/{world.rid}/messages",
                world.olga,
                {"kind": "task", "body": {"text": "ship it"}, "mentions": [reg["participant_id"]]},
            )
        )
        async with world.client() as c:
            task = await c.task_for(Message.from_json(sent))
            final = await c.wait_task(task["id"], max_wait=20)
        assert final["state"] == "completed"
        assert final["artifacts"][0]["parts"][0]["text"] == "Graphy says: SHIP IT"


async def test_embedded_servers_leave_process_signal_handlers_alone() -> None:
    """Several servers started and stopped in one process must not stack signal handlers: a
    handler left bound to a stopped server makes sse-starlette cut every other stream short."""

    async def hello(req: A2ARequest) -> str:
        return "hi"

    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    ports = [free_port(), free_port()]
    apps = [
        build_a2a_app(hello, name=f"A{i}", url=f"http://127.0.0.1:{p}", skills=[Skill("a.b", "AB")])
        for i, p in enumerate(ports)
    ]
    tasks = [asyncio.create_task(serve(a, port=p)) for a, p in zip(apps, ports, strict=True)]
    async with httpx.AsyncClient() as http:

        async def up(port: int) -> bool:
            try:
                return (
                    await http.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
                ).is_success
            except httpx.HTTPError:
                return False

        for p in ports:
            await until(lambda p=p: up(p), what=f"server on {p}")
    for t in tasks:  # stop in the order that used to leave a stale handler behind
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t
    assert {s: signal.getsignal(s) for s in before} == before
