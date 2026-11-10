"""Expose a LangGraph graph (or any async callable) as a standard A2A server.

This is the "I have a LangGraph agent, make it A2A" path: the result is an ordinary A2A agent
(Agent Card, JSON-RPC, SSE streaming, A2A 1.0 and 0.3) that a team registers in Setu by pasting
its card URL. Setu then calls it like any other registered agent, with policy, delegation,
tracing and notifications applied. Requires `pip install setu-sdk[serve]`.

    async def score(req: A2ARequest) -> dict:
        await req.progress("looking up the claim")
        return {"risk": "low"}

    app = build_a2a_app(score, name="Risk-Agent", url="http://localhost:9101",
                        skills=[Skill("risk.score", "Risk scoring", tags=["risk"])])
    run(app, port=9101)             # card at http://localhost:9101/.well-known/agent-card.json

or, for a compiled graph:  `build_a2a_app(graph_handler(graph), ...)`.
What the handler returns becomes the task's artifact: a str is a text part, a mapping is a data
part, a list of A2A part dicts is used as given. An exception fails the task with its message.
"""

import asyncio
import contextlib
import socket
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as pb
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict, ParseDict  # type: ignore[import-untyped]
from starlette.types import ASGIApp, Receive, Scope, Send

RPC_PATH = "/rpc"
CARD_PATH = "/.well-known/agent-card.json"
LEGACY_CARD_PATH = "/.well-known/agent.json"


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str = ""
    tags: Sequence[str] = ()


@dataclass
class A2ARequest:
    """What the handler is given for one incoming message."""

    text: str
    data: list[Any]  # the values of any data parts, in order
    parts: list[dict[str, Any]]  # every part, in the 1.0 neutral shape
    context_id: str
    task_id: str
    metadata: dict[str, Any]  # message metadata (Setu puts room, task and trace ids under "setu")
    headers: Mapping[str, str]  # HTTP headers of the call (e.g. `traceparent` from Setu)
    progress: Callable[[str], Awaitable[None]] = field(repr=False, default=lambda _t: _noop())

    @property
    def setu(self) -> dict[str, Any]:
        inner = self.metadata.get("setu")
        return inner if isinstance(inner, dict) else {}


async def _noop() -> None:
    return None


Result = str | Mapping[str, Any] | Sequence[Mapping[str, Any]]
Handler = Callable[[A2ARequest], Awaitable[Result]]


def graph_handler(
    graph: Any,
    *,
    to_input: Callable[[A2ARequest], Mapping[str, Any]] | None = None,
    to_output: Callable[[Mapping[str, Any]], Result] | None = None,
) -> Handler:
    """Turn a compiled LangGraph graph into a handler. The graph is invoked with
    `{"text", "data", "metadata"}` (override with `to_input`); the reply is the final state's
    `output` / `result` / `text` (override with `to_output`), else the state itself."""

    def default_in(req: A2ARequest) -> Mapping[str, Any]:
        return {"text": req.text, "data": req.data, "metadata": req.metadata}

    def default_out(state: Mapping[str, Any]) -> Result:
        for key in ("output", "result", "text"):
            value = state.get(key)
            if isinstance(value, str | Mapping) and value:
                return value
        return {k: v for k, v in state.items() if k != "metadata"}

    make_input = to_input or default_in
    make_output = to_output or default_out

    async def run_graph(req: A2ARequest) -> Result:
        config = {"configurable": {"thread_id": req.task_id, "a2a": req}}
        state = await graph.ainvoke(dict(make_input(req)), config)
        return make_output(state)

    return run_graph


def _parts(result: Result) -> list[pb.Part]:
    if isinstance(result, str):
        return [pb.Part(text=result)]
    if isinstance(result, Mapping):
        return [ParseDict({"data": dict(result)}, pb.Part())]
    return [ParseDict(dict(p), pb.Part()) for p in result]


class _Executor(AgentExecutor):
    def __init__(self, handler: Handler, artifact_name: str) -> None:
        self._handler = handler
        self._artifact_name = artifact_name
        self._running: dict[str, asyncio.Task[Result]] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        assert message is not None
        task = context.current_task or new_task_from_user_message(message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        raw = [MessageToDict(p) for p in message.parts]
        meta = MessageToDict(message.metadata) if message.HasField("metadata") else {}
        headers = context.call_context.state.get("headers", {}) if context.call_context else {}

        async def progress(text: str) -> None:
            await updater.update_status(
                pb.TASK_STATE_WORKING, updater.new_agent_message([new_text_part(text)])
            )

        request = A2ARequest(
            text=context.get_user_input(),
            data=[p["data"] for p in raw if "data" in p],
            parts=raw,
            context_id=task.context_id,
            task_id=task.id,
            metadata=meta,
            headers=headers,
            progress=progress,
        )
        run = asyncio.ensure_future(self._handler(request))
        self._running[task.id] = run
        try:
            result = await run
        except asyncio.CancelledError:
            if run.cancelled():  # cancelled through `cancel()`, which answers the caller
                return
            raise
        except Exception as exc:
            await updater.failed(updater.new_agent_message([new_text_part(f"{exc}")]))
        else:
            await updater.add_artifact(_parts(result), name=self._artifact_name, last_chunk=True)
            await updater.complete()
        finally:
            self._running.pop(task.id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        run = self._running.get(context.task_id or "")
        if run is not None:
            run.cancel()
        await TaskUpdater(event_queue, context.task_id or "", context.context_id or "").cancel()


class _Bearer:
    """Rejects JSON-RPC calls without the expected bearer token (the card stays public)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == RPC_PATH:
            sent = dict((k.decode().lower(), v.decode()) for k, v in scope["headers"])
            if sent.get("authorization") != f"Bearer {self.token}":
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _card(
    *,
    name: str,
    description: str,
    version: str,
    base_url: str,
    skills: Sequence[Skill],
    bearer: bool,
) -> dict[str, Any]:
    rpc = base_url.rstrip("/") + RPC_PATH
    card: dict[str, Any] = {
        "name": name,
        "description": description,
        "version": version,
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        # The A2A spec requires a description and tags on every skill, so default them.
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description or s.name,
                "tags": list(s.tags) or [s.id.split(".")[0]],
            }
            for s in skills
        ],
        "supportedInterfaces": [
            {"url": rpc, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": rpc, "protocolBinding": "JSONRPC", "protocolVersion": "0.3"},
        ],
    }
    if bearer:
        card["securitySchemes"] = {
            "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}},
        }
        card["securityRequirements"] = [{"schemes": {"bearer": {"list": []}}}]
    return card


def _card_v03(card: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        k: v
        for k, v in card.items()
        if k not in ("supportedInterfaces", "securitySchemes", "securityRequirements")
    }
    out |= {
        "protocolVersion": "0.3.0",
        "url": card["supportedInterfaces"][0]["url"],
        "preferredTransport": "JSONRPC",
    }
    if "securitySchemes" in card:
        out["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
        out["security"] = [{"bearer": []}]
    return out


def build_a2a_app(
    handler: Handler,
    *,
    name: str,
    url: str,
    skills: Sequence[Skill | Mapping[str, Any]],
    description: str = "",
    version: str = "1.0.0",
    bearer_token: str | None = None,
    artifact_name: str = "result",
) -> FastAPI:
    """An ASGI app serving `handler` as an A2A agent reachable at `url` (its public base URL,
    which goes into the Agent Card). `bearer_token`, if set, is required on every JSON-RPC call;
    register the agent in Setu with the same token so Setu can call it."""
    specs = [s if isinstance(s, Skill) else Skill(**s) for s in skills]
    card = _card(
        name=name,
        description=description or f"{name} (served with setu-sdk)",
        version=version,
        base_url=url,
        skills=specs,
        bearer=bearer_token is not None,
    )
    app = FastAPI(title=name)

    @app.get(CARD_PATH)
    async def agent_card() -> dict[str, Any]:
        return card

    @app.get(LEGACY_CARD_PATH)
    async def agent_card_v03() -> dict[str, Any]:
        return _card_v03(card)

    request_handler = DefaultRequestHandlerV2(
        agent_executor=_Executor(handler, artifact_name),
        task_store=InMemoryTaskStore(),
        agent_card=ParseDict(card, pb.AgentCard()),
    )
    for route in create_jsonrpc_routes(request_handler, rpc_url=RPC_PATH, enable_v0_3_compat=True):
        app.router.routes.append(route)
    if bearer_token is not None:
        app.add_middleware(_Bearer, token=bearer_token)
    return app


def free_port() -> int:
    """A port that is free right now (for tests and demos that start several servers)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run(app: FastAPI, *, host: str = "127.0.0.1", port: int = 9000) -> None:
    """Serve until interrupted."""
    uvicorn.run(app, host=host, port=port, log_level="warning")


class EmbeddedServer(uvicorn.Server):
    """A uvicorn server for running inside a larger program (tests, demos, a launcher).

    It never touches the process's signal handlers. Those are process-global: several servers
    started in one process would install and restore them in the wrong order and leave the
    process holding a handler bound to a stopped server, which makes `sse-starlette` (used by
    A2A streaming) believe every other server is shutting down and cut its streams short.
    Stop it by setting `should_exit` or cancelling the task running `serve()`.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(app: FastAPI, *, host: str = "127.0.0.1", port: int = 9000) -> None:
    """Serve inside a running event loop (cancel the task to stop it)."""
    await EmbeddedServer(uvicorn.Config(app, host=host, port=port, log_level="warning")).serve()


__all__ = [
    "A2ARequest",
    "EmbeddedServer",
    "Handler",
    "Skill",
    "build_a2a_app",
    "free_port",
    "graph_handler",
    "run",
    "serve",
]
