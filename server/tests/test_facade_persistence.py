"""Facade tasks and push-notification webhooks live in Postgres (decision 124): they survive a
restart, belong to their caller, and webhooks are SSRF-guarded with secrets encrypted at rest."""

import asyncio
import time
from typing import Any

import asyncpg
from fastapi import FastAPI, Request

from tests.a2a_agents import build_v1_agent
from tests.test_a2a_flow import hub, wait_for
from tests.test_facade import (
    RISK,
    RISK_SKILLS,
    World,
    agents,
    message_v1,
    result_task,
    rpc_v1,
    state_of,
)


def run_task(world: World, client: Any, handle: str, text_: str = "hi") -> dict[str, Any]:
    root = world.grant_root()
    payload, hdrs = rpc_v1(
        "SendMessage",
        message_v1(world.room1["id"], text_, skill="risk.score", delegation_id=root["id"]),
    )
    return result_task(world.call(client, world.key_a, handle, payload, hdrs))


def test_tasks_survive_a_restart_and_stay_private(migrated_database_url: str) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w)
        handle = f"risk-{w.tag}"
        task = run_task(world, client, handle)
        assert state_of(task) == "completed" and task["artifacts"]
        tid = task["id"]
        key_a = world.key_a

        # Another caller cannot see it, even with the right id.
        other = world.key_b
        payload, hdrs = rpc_v1("GetTask", {"id": tid})
        stranger = world.call(client, other, handle, payload, hdrs)
        assert "error" in stranger.json()

    # A new process on the same database: the task is still there.
    with hub(migrated_database_url, agents(risk)) as (_, _, client):
        payload, hdrs = rpc_v1("GetTask", {"id": tid})
        got = result_task(
            client.post(f"/a2a/{handle}", json=payload, headers={"X-API-Key": key_a, **hdrs})
        )
        assert got["id"] == tid and state_of(got) == "completed"
        assert got["artifacts"][0]["parts"]

        payload, hdrs = rpc_v1("ListTasks", {"contextId": world.room1["id"]})
        listed = client.post(
            f"/a2a/{handle}", json=payload, headers={"X-API-Key": key_a, **hdrs}
        ).json()
        assert [t["id"] for t in listed["result"]["tasks"]] == [tid]


def webhook_app(sink: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/cb")
    async def cb(request: Request) -> dict[str, str]:
        sink.append({"headers": dict(request.headers), "body": await request.json()})
        return {"ok": "1"}

    return app


def test_push_notifications_reach_the_webhook(migrated_database_url: str) -> None:
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    got: list[dict[str, Any]] = []
    apps = {**agents(risk), "hook.test": webhook_app(got)}
    with hub(migrated_database_url, apps) as (w, _, client):
        world = World(w)
        root = world.grant_root()
        params = message_v1(world.room1["id"], "hi", skill="risk.score", delegation_id=root["id"])
        params["configuration"] = {
            "returnImmediately": True,
            "taskPushNotificationConfig": {
                "url": "http://hook.test/cb",
                "token": "tok-123",
                "authentication": {"scheme": "Bearer", "credentials": "hook-secret"},
            },
        }
        payload, hdrs = rpc_v1("SendMessage", params)
        sent = result_task(world.call(client, world.key_a, f"risk-{w.tag}", payload, hdrs))
        tid = sent["id"]

        def done() -> bool:
            return any(
                "COMPLETED" in str(n["body"]) or "completed" in str(n["body"]).lower() for n in got
            )

        wait_for(done, what="completed webhook")
        first = got[0]
        assert first["headers"]["x-a2a-notification-token"] == "tok-123"
        assert first["headers"]["authorization"] == "Bearer hook-secret"
        assert tid in str(got)

        # The secret is encrypted at rest.
        async def stored() -> str:
            conn = await asyncpg.connect(migrated_database_url.replace("+asyncpg", ""))
            try:
                return str(
                    await conn.fetchval(
                        "select secret::text from facade_push_configs where wire_task_id=$1", tid
                    )
                )
            finally:
                await conn.close()

        raw = asyncio.run(stored())
        assert "hook-secret" not in raw and "tok-123" not in raw and "ct" in raw


def test_unsafe_webhook_urls_are_not_called(migrated_database_url: str, monkeypatch: Any) -> None:
    """Webhook targets go through the same URL guard as agent URLs (here: reject hook.test)."""
    from app.a2a.ssrf import UnsafeUrlError, validate_agent_url

    async def guard(url: str, *, allow_private: bool) -> None:
        if "hook.test" in url:
            raise UnsafeUrlError("non-public address")
        await validate_agent_url(url, allow_private=True)

    monkeypatch.setattr("app.main.validate_agent_url", guard)
    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    got: list[dict[str, Any]] = []
    with hub(migrated_database_url, {**agents(risk), "hook.test": webhook_app(got)}) as (
        w,
        _,
        client,
    ):
        world = World(w)
        root = world.grant_root()
        params = message_v1(world.room1["id"], "hi", skill="risk.score", delegation_id=root["id"])
        params["configuration"] = {
            "taskPushNotificationConfig": {"url": "http://hook.test/cb", "token": "x"}
        }
        payload, hdrs = rpc_v1("SendMessage", params)
        resp = world.call(client, world.key_a, f"risk-{w.tag}", payload, hdrs).json()
        assert (
            resp["error"]["code"] == -32602 and "push notification URL" in resp["error"]["message"]
        )
        time.sleep(0.3)
        assert got == []
