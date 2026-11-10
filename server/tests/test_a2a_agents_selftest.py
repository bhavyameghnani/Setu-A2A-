"""Self-tests for the A2A fixture agents."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tests.a2a_agents import HostRouter, build_v03_agent, build_v1_agent

V1 = {"A2A-Version": "1.0"}


def _msg(v1: bool, text: str) -> dict[str, Any]:
    if v1:
        return {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}]}
    return {
        "kind": "message",
        "messageId": "m1",
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
    }


def _rpc(v1: bool, op: str, params: dict[str, Any]) -> dict[str, Any]:
    names = {
        "stream": ("SendStreamingMessage", "message/stream"),
        "send": ("SendMessage", "message/send"),
        "cancel": ("CancelTask", "tasks/cancel"),
        "get": ("GetTask", "tasks/get"),
    }
    return {"jsonrpc": "2.0", "id": 1, "method": names[op][0 if v1 else 1], "params": params}


def _make(kind: str, **kw: Any) -> Any:
    base = "http://a.test"
    return (build_v1_agent if kind == "v1" else build_v03_agent)(base, name="Risk", **kw)


async def _stream(c: httpx.AsyncClient, v1: bool, text: str, **hdr: str) -> list[dict[str, Any]]:
    out = []
    headers = {**(V1 if v1 else {}), **hdr}
    body = _rpc(v1, "stream", {"message": _msg(v1, text)})
    async with c.stream("POST", "http://a.test/rpc", json=body, headers=headers) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:])["result"])
    return out


def _flat(v1: bool, events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Normalise to (kind, detail) pairs."""
    out = []
    for ev in events:
        if v1:
            ((k, v),) = ev.items()
            if k == "task":
                out.append(("task", v["status"]["state"]))
            elif k == "statusUpdate":
                out.append(("status", v["status"]["state"].removeprefix("TASK_STATE_").lower()))
            else:
                out.append(("artifact", v["artifact"]["name"]))
        else:
            k = ev["kind"]
            if k == "task":
                out.append(("task", ev["status"]["state"]))
            elif k == "status-update":
                out.append(("status", ev["status"]["state"].replace("-", "_")))
            else:
                out.append(("artifact", ev["artifact"]["name"]))
    return out


def _status_text(v1: bool, ev: dict[str, Any]) -> str:
    st = (ev["statusUpdate"] if v1 else ev)["status"]
    return st["message"]["parts"][0]["text"]


@pytest.fixture(params=["v1", "v03"])
def kind(request: pytest.FixtureRequest) -> str:
    return request.param


async def test_default_and_received(kind: str) -> None:
    v1 = kind == "v1"
    app = _make(kind)
    async with httpx.AsyncClient(transport=HostRouter({"a.test": app})) as c:
        evs = await _stream(c, v1, "hello", traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01")
    assert _flat(v1, evs) == [
        ("task", "TASK_STATE_SUBMITTED" if v1 else "submitted"),
        ("status", "working"),
        ("artifact", "result"),
        ("status", "completed"),
    ]
    art = evs[2]["artifactUpdate"] if v1 else evs[2]
    assert art["artifact"]["parts"][0]["text"] == "Risk handled: hello"
    assert art["lastChunk"] is True
    if not v1:
        assert evs[1]["final"] is False and evs[3]["final"] is True
    (rec,) = app.state.received
    assert rec.method == ("SendStreamingMessage" if v1 else "message/stream")
    assert rec.headers["traceparent"].startswith("00-")
    assert rec.body["params"]["message"]["messageId"] == "m1"


async def test_fail_input_multi(kind: str) -> None:
    v1 = kind == "v1"
    async with httpx.AsyncClient(transport=HostRouter({"a.test": _make(kind)})) as c:
        fail = await _stream(c, v1, "fail please")
        inp = await _stream(c, v1, "input needed")
        multi = await _stream(c, v1, "multi x")
    assert [s for _, s in _flat(v1, fail)][1:] == ["working", "failed"]
    assert _status_text(v1, fail[-1]) == "boom"
    assert [s for _, s in _flat(v1, inp)][1:] == ["working", "input_required"]
    assert _status_text(v1, inp[-1]) == "which claim id?"
    assert _flat(v1, multi)[1:] == [
        ("status", "working"),
        ("artifact", "part-1"),
        ("artifact", "part-2"),
        ("status", "completed"),
    ]


async def test_send_non_streaming(kind: str) -> None:
    v1 = kind == "v1"
    async with httpx.AsyncClient(transport=HostRouter({"a.test": _make(kind)})) as c:
        r = await c.post(
            "http://a.test/rpc",
            json=_rpc(v1, "send", {"message": _msg(v1, "hi")}),
            headers=V1 if v1 else {},
        )
    res = r.json()["result"]
    task = res["task"] if v1 else res
    assert task["status"]["state"] == ("TASK_STATE_COMPLETED" if v1 else "completed")
    assert task["artifacts"][0]["name"] == "result"


async def test_slow_cancel(kind: str) -> None:
    v1 = kind == "v1"
    app = _make(kind)
    hdr = V1 if v1 else {}
    events: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=HostRouter({"a.test": app})) as c:
        body = _rpc(v1, "stream", {"message": _msg(v1, "slow job")})
        async with c.stream("POST", "http://a.test/rpc", json=body, headers=hdr) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])["result"]
                events.append(ev)
                if len(events) == 2:  # task, working -> now cancel
                    task = ev["statusUpdate"] if v1 else ev
                    cr = await asyncio.wait_for(
                        c.post(
                            "http://a.test/rpc",
                            json=_rpc(v1, "cancel", {"id": task["taskId"]}),
                            headers=hdr,
                        ),
                        5,
                    )
                    assert "error" not in cr.json()
        missing = await c.post(
            "http://a.test/rpc", json=_rpc(v1, "cancel", {"id": "nope"}), headers=hdr
        )
        assert missing.json()["error"]["code"] == -32001
    flat = _flat(v1, events)
    assert flat[:2] == [
        ("task", "TASK_STATE_SUBMITTED" if v1 else "submitted"),
        ("status", "working"),
    ]
    assert flat[-1] == ("status", "canceled")
    assert not any(k == "artifact" for k, _ in flat)


async def test_unknown_method_v03() -> None:
    async with httpx.AsyncClient(transport=HostRouter({"a.test": _make("v03")})) as c:
        r = await c.post("http://a.test/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "zzz"})
    assert r.json()["error"]["code"] == -32601


async def test_bearer(kind: str) -> None:
    v1 = kind == "v1"
    app = _make(kind, bearer_token="s3cret")
    async with httpx.AsyncClient(transport=HostRouter({"a.test": app})) as c:
        body = _rpc(v1, "send", {"message": _msg(v1, "hi")})
        hdr = V1 if v1 else {}
        assert (await c.post("http://a.test/rpc", json=body, headers=hdr)).status_code == 401
        assert not app.state.received
        ok = await c.post(
            "http://a.test/rpc", json=body, headers={**hdr, "Authorization": "Bearer s3cret"}
        )
        assert ok.status_code == 200 and len(app.state.received) == 1
        card = (await c.get("http://a.test/.well-known/agent-card.json")).json()
    if v1:
        assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
        assert card["securityRequirements"]
    else:
        assert card["securitySchemes"]["bearer"] == {"type": "http", "scheme": "bearer"}
        assert card["security"] == [{"bearer": []}]


async def test_dynamic_card_and_shape(kind: str) -> None:
    app = _make(kind)
    async with httpx.AsyncClient(transport=HostRouter({"a.test": app})) as c:
        u = "http://a.test/.well-known/agent-card.json"
        card = (await c.get(u)).json()
        app.state.skills = [("s2", "Second", ["x", "y"])]
        card2 = (await c.get(u)).json()
    assert app.state.card_hits == 2
    assert card["name"] == "Risk" and card["capabilities"]["streaming"] is True
    assert [s["id"] for s in card["skills"]] == ["echo"]
    assert card2["skills"][0]["id"] == "s2" and card2["skills"][0]["tags"] == ["x", "y"]
    if kind == "v1":
        i = card["supportedInterfaces"][0]
        assert i == {
            "url": "http://a.test/rpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    else:
        assert card["url"] == "http://a.test/rpc" and card["protocolVersion"] == "0.3.0"
        assert card["preferredTransport"] == "JSONRPC"


async def test_v03_custom_card_path() -> None:
    app = build_v03_agent("http://a.test", card_path="/.well-known/agent.json")
    async with httpx.AsyncClient(transport=HostRouter({"a.test": app})) as c:
        assert (await c.get("http://a.test/.well-known/agent.json")).status_code == 200
        assert (await c.get("http://a.test/.well-known/agent-card.json")).status_code == 404


async def test_host_router_down_and_unknown() -> None:
    router = HostRouter({"a.test": _make("v1")})
    async with httpx.AsyncClient(transport=router) as c:
        assert (await c.get("http://a.test/.well-known/agent-card.json")).status_code == 200
        router.down.add("a.test")
        with pytest.raises(httpx.ConnectError):
            await c.get("http://a.test/.well-known/agent-card.json")
        with pytest.raises(httpx.ConnectError):
            await c.get("http://nowhere.test/x")
    assert router.calls[0] == ("a.test", "GET", "/.well-known/agent-card.json")
    assert [h for h, _, _ in router.calls] == ["a.test", "a.test", "nowhere.test"]
