"""In-process A2A agent fixtures (A2A 1.0 via a2a-sdk, A2A 0.3 hand-rolled) for tests.

Behaviour is selected by the first text part of the user message:
``fail*``, ``input*``, ``slow*``, ``multi*``, anything else is the default echo.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx
from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as pb
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict

CARD_PATH = "/.well-known/agent-card.json"
SLOW_TIMEOUT = 30.0

Skill = tuple[str, str, list[str]]  # (id, name, tags)


@dataclass
class Received:
    """One record per JSON-RPC POST the agent received (authorized requests only)."""

    method: str
    headers: dict[str, str]
    body: dict[str, Any]


def _mode(text: str) -> str:
    for prefix in ("fail", "input", "slow", "multi"):
        if text.startswith(prefix):
            return prefix
    return "default"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- middleware


class _RpcGate:
    """Pure-ASGI middleware: bearer enforcement + recording of JSON-RPC POSTs."""

    def __init__(self, app: Any, state: Any, rpc_path: str, token: str | None) -> None:
        self.app = app
        self.state = state
        self.rpc_path = rpc_path
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self.rpc_path:
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if self.token is not None and headers.get("authorization") != f"Bearer {self.token}":
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        raw = b"".join(chunks)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        if isinstance(body, dict):
            self.state.received.append(Received(str(body.get("method", "")), headers, body))
        sent = False

        async def replay() -> Any:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _init_state(app: FastAPI, name: str, skills: list[Skill] | None) -> None:
    app.state.received = []
    app.state.skills = list(skills) if skills is not None else [("echo", "Echo", ["echo"])]
    app.state.card_hits = 0
    app.state.agent_name = name


def _wrap(app: FastAPI, bearer_token: str | None) -> FastAPI:
    # Wrap the ASGI entry so the gate sees raw requests; keep the FastAPI object returned.
    app.add_middleware(_RpcGate, state=app.state, rpc_path="/rpc", token=bearer_token)
    return app


# --------------------------------------------------------------------------- v1 agent


def _v1_card(app: FastAPI, base_url: str, bearer_token: str | None) -> pb.AgentCard:
    card = pb.AgentCard(
        name=app.state.agent_name,
        description=f"{app.state.agent_name} test agent",
        version="1.0.0",
        supported_interfaces=[
            pb.AgentInterface(
                url=base_url + "/rpc", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=pb.AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[pb.AgentSkill(id=i, name=n, description=n, tags=t) for i, n, t in app.state.skills],
    )
    if bearer_token is not None:
        card.security_schemes["bearer"].CopyFrom(
            pb.SecurityScheme(http_auth_security_scheme=pb.HTTPAuthSecurityScheme(scheme="Bearer"))
        )
        card.security_requirements.append(
            pb.SecurityRequirement(schemes={"bearer": pb.StringList(list=[])})
        )
    return card


class _V1Executor(AgentExecutor):
    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.delay = delay
        self.cancel_events: dict[str, asyncio.Event] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        u = TaskUpdater(event_queue, task.id, task.context_id)
        text = context.get_user_input()
        mode = _mode(text)
        await u.start_work()
        await asyncio.sleep(self.delay)
        if mode == "fail":
            await u.failed(u.new_agent_message([new_text_part("boom")]))
        elif mode == "input":
            await u.requires_input(u.new_agent_message([new_text_part("which claim id?")]))
        elif mode == "slow":
            ev = self.cancel_events.setdefault(task.id, asyncio.Event())
            try:
                await asyncio.wait_for(ev.wait(), SLOW_TIMEOUT)
            except TimeoutError:
                await u.cancel()
        elif mode == "multi":
            for n in ("part-1", "part-2"):
                await u.add_artifact([new_text_part(f"{n} of {text}")], name=n, last_chunk=True)
            await u.complete()
        else:
            await u.add_artifact(
                [new_text_part(f"{self.name} handled: {text}")], name="result", last_chunk=True
            )
            await u.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        tid = context.task_id or ""
        await TaskUpdater(event_queue, tid, context.context_id or "").cancel()
        self.cancel_events.setdefault(tid, asyncio.Event()).set()


def build_v1_agent(
    base_url: str,
    *,
    name: str = "Agent",
    skills: list[Skill] | None = None,
    bearer_token: str | None = None,
    delay: float = 0.02,
) -> FastAPI:
    app = FastAPI()
    _init_state(app, name, skills)
    handler = DefaultRequestHandlerV2(
        agent_executor=_V1Executor(name, delay),
        task_store=InMemoryTaskStore(),
        agent_card=_v1_card(app, base_url, bearer_token),
    )

    @app.get(CARD_PATH)
    async def card() -> JSONResponse:
        app.state.card_hits += 1
        return JSONResponse(MessageToDict(_v1_card(app, base_url, bearer_token)))

    for route in create_jsonrpc_routes(handler, rpc_url="/rpc", enable_v0_3_compat=False):
        app.router.routes.append(route)
    return _wrap(app, bearer_token)


# --------------------------------------------------------------------------- v0.3 agent


def _text_of(msg: dict[str, Any]) -> str:
    for p in msg.get("parts", []):
        if p.get("kind", "text") == "text" and "text" in p:
            return str(p["text"])
    return ""


def _v03_card(app: FastAPI, base_url: str, bearer_token: str | None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "protocolVersion": "0.3.0",
        "name": app.state.agent_name,
        "description": f"{app.state.agent_name} test agent",
        "version": "1.0.0",
        "url": base_url + "/rpc",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {"id": i, "name": n, "description": n, "tags": t} for i, n, t in app.state.skills
        ],
    }
    if bearer_token is not None:
        card["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
        card["security"] = [{"bearer": []}]
    return card


def build_v03_agent(
    base_url: str,
    *,
    name: str = "Agent",
    skills: list[Skill] | None = None,
    bearer_token: str | None = None,
    delay: float = 0.02,
    card_path: str = CARD_PATH,
) -> FastAPI:
    app = FastAPI()
    _init_state(app, name, skills)
    tasks: dict[str, dict[str, Any]] = {}
    cancel_events: dict[str, asyncio.Event] = {}

    @app.get(card_path)
    async def card() -> JSONResponse:
        app.state.card_hits += 1
        return JSONResponse(_v03_card(app, base_url, bearer_token))

    def status_ev(t: dict[str, Any], state: str, final: bool, text: str | None = None) -> dict:
        status: dict[str, Any] = {"state": state, "timestamp": _now()}
        if text is not None:
            status["message"] = {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "agent",
                "taskId": t["id"],
                "contextId": t["contextId"],
                "parts": [{"kind": "text", "text": text}],
            }
        t["status"] = status
        return {
            "kind": "status-update",
            "taskId": t["id"],
            "contextId": t["contextId"],
            "final": final,
            "status": status,
        }

    def artifact_ev(t: dict[str, Any], art_name: str, text: str) -> dict:
        art = {
            "artifactId": str(uuid.uuid4()),
            "name": art_name,
            "parts": [{"kind": "text", "text": text}],
        }
        t.setdefault("artifacts", []).append(art)
        return {
            "kind": "artifact-update",
            "taskId": t["id"],
            "contextId": t["contextId"],
            "append": False,
            "lastChunk": True,
            "artifact": art,
        }

    async def run(msg: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        tid = msg.get("taskId") or str(uuid.uuid4())
        cid = msg.get("contextId") or str(uuid.uuid4())
        t = tasks.get(tid) or {"kind": "task", "id": tid, "contextId": cid, "history": []}
        t["history"].append(msg)
        tasks[tid] = t
        t["status"] = {"state": "submitted"}
        yield dict(t)
        text = _text_of(msg)
        mode = _mode(text)
        yield status_ev(t, "working", False)
        await asyncio.sleep(delay)
        if mode == "fail":
            yield status_ev(t, "failed", True, "boom")
        elif mode == "input":
            yield status_ev(t, "input-required", True, "which claim id?")
        elif mode == "slow":
            ev = cancel_events.setdefault(tid, asyncio.Event())
            try:
                await asyncio.wait_for(ev.wait(), SLOW_TIMEOUT)
            except TimeoutError:
                pass
            yield status_ev(t, "canceled", True)
        elif mode == "multi":
            for n in ("part-1", "part-2"):
                yield artifact_ev(t, n, f"{n} of {text}")
            yield status_ev(t, "completed", True)
        else:
            yield artifact_ev(t, "result", f"{name} handled: {text}")
            yield status_ev(t, "completed", True)

    def err(rid: Any, code: int, message: str) -> JSONResponse:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
        )

    @app.post("/rpc", response_model=None)
    async def rpc(req: Request) -> Any:
        b = await req.json()
        method, rid = b.get("method"), b.get("id")
        params = b.get("params") or {}
        if method == "message/send":
            async for _ in run(params["message"]):
                pass
            last = next(reversed(tasks.values()))
            return {"jsonrpc": "2.0", "id": rid, "result": last}
        if method == "message/stream":

            async def gen() -> AsyncIterator[str]:
                async for ev in run(params["message"]):
                    yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': ev})}\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        if method in ("tasks/get", "tasks/cancel"):
            t = tasks.get(params.get("id", ""))
            if t is None:
                return err(rid, -32001, "Task not found")
            if method == "tasks/cancel":
                cancel_events.setdefault(t["id"], asyncio.Event()).set()
                if t["status"]["state"] in ("working", "submitted"):
                    t["status"] = {"state": "canceled", "timestamp": _now()}
            return {"jsonrpc": "2.0", "id": rid, "result": t}
        return err(rid, -32601, "Method not found")

    return _wrap(app, bearer_token)


# --------------------------------------------------------------------------- transport


class _QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue: asyncio.Queue[bytes | None], task: asyncio.Task[None]) -> None:
        self.queue = queue
        self.task = task

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (chunk := await self.queue.get()) is not None:
            yield chunk

    async def aclose(self) -> None:
        if not self.task.done():
            self.task.cancel()


class HostRouter(httpx.AsyncBaseTransport):
    """Routes requests by URL host to in-process ASGI apps (response bodies stream)."""

    def __init__(self, apps: dict[str, Any]) -> None:
        self.apps = apps
        self.down: set[str] = set()
        self.calls: list[tuple[str, str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append((host, request.method, request.url.path))
        app = self.apps.get(host)
        if app is None or host in self.down:
            raise httpx.ConnectError(f"connection refused: {host}", request=request)
        body = await request.aread()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": unquote(request.url.path),
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "root_path": "",
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "server": (host, request.url.port or 80),
            "client": ("127.0.0.1", 12345),
        }
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        started: asyncio.Future[tuple[int, list[tuple[bytes, bytes]]]] = (
            asyncio.get_running_loop().create_future()
        )
        sent_body = False
        disconnect = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg: dict[str, Any]) -> None:
            if msg["type"] == "http.response.start":
                if not started.done():
                    started.set_result((msg["status"], list(msg.get("headers", []))))
            elif msg["type"] == "http.response.body":
                if msg.get("body"):
                    queue.put_nowait(msg["body"])
                if not msg.get("more_body"):
                    queue.put_nowait(None)

        async def runner() -> None:
            try:
                await app(scope, receive, send)
            except BaseException as exc:
                if not started.done():
                    started.set_exception(exc)
                raise
            finally:
                queue.put_nowait(None)
                disconnect.set()

        task = asyncio.create_task(runner())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        status, headers = await started
        return httpx.Response(status, headers=headers, stream=_QueueStream(queue, task))


__all__: list[str] = [
    "HostRouter",
    "Received",
    "Skill",
    "build_v03_agent",
    "build_v1_agent",
]
