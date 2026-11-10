"""HTTP side of the A2A facade (`app.a2a.facade` holds the logic).

    GET  /a2a/{handle}/.well-known/agent-card.json   proxied card (A2A 1.0, lists 0.3 too)
    GET  /a2a/{handle}/.well-known/agent.json        the same card in the 0.3 shape
    POST /a2a/{handle}                                JSON-RPC 1.0 and 0.3, SSE streaming

Both need a Setu credential: handles are only unique within an organization, and an unknown
or invisible handle answers 404 the same way.
"""

import uuid
from typing import Any

from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_jsonrpc_routes
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from app.a2a import facade, facade_store
from app.a2a.facade import current_principal, current_trace
from app.api.deps import resolve_principal
from app.core.auth import AuthError, Principal
from app.core.tracing import parse_traceparent
from app.db.models import Agent, Participant


def _bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return None


async def _authenticate(request: Request) -> Principal | JSONResponse:
    state = request.app.state
    try:
        async with state.sessionmaker() as s:
            principal = await resolve_principal(
                s,
                state.jwt_secret,
                _bearer(request.headers.get("authorization")),
                request.headers.get("x-api-key"),
            )
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if not principal.has_scope("rooms:read"):
        return JSONResponse({"detail": "api key lacks scope rooms:read"}, status_code=403)
    return principal


async def _lookup(request: Request, principal: Principal, handle: str) -> tuple[Participant, Agent]:
    async with request.app.state.sessionmaker() as s:
        row = (
            await s.execute(
                select(Participant, Agent)
                .join(Agent, Agent.participant_id == Participant.id)
                .where(
                    Participant.org_id == principal.org_id,
                    Participant.handle == handle,
                    Participant.status == "active",
                    Agent.transport == "a2a_remote",
                    Agent.enabled.is_(True),
                )
            )
        ).first()
    if row is None:
        raise LookupError(handle)
    return row[0], row[1]


def _base(request: Request, handle: str) -> str:
    configured = request.app.state.settings.public_url
    base = configured.rstrip("/") if configured else str(request.base_url).rstrip("/")
    return f"{base}/a2a/{handle}"


_NOT_FOUND = JSONResponse({"detail": "agent not found"}, status_code=404)


async def _card(request: Request) -> JSONResponse:
    handle = request.path_params["handle"]
    principal = await _authenticate(request)
    if isinstance(principal, JSONResponse):
        return principal
    try:
        participant, agent = await _lookup(request, principal, handle)
    except LookupError:
        return _NOT_FOUND
    build = facade.card_v03 if request.url.path.endswith("/agent.json") else facade.card_v1
    return JSONResponse(build(participant, agent, _base(request, handle)))


class RpcEndpoint:
    """ASGI app for `POST /a2a/{handle}`: authenticate, find the agent, then hand the request to
    the a2a-sdk JSON-RPC routes with an executor bound to this caller."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        handle = request.path_params["handle"]
        principal = await _authenticate(request)
        if isinstance(principal, JSONResponse):
            await principal(scope, receive, send)
            return
        if not principal.has_scope("messages:send"):
            denied = JSONResponse({"detail": "api key lacks scope messages:send"}, status_code=403)
            await denied(scope, receive, send)
            return
        try:
            participant, agent = await _lookup(request, principal, handle)
        except LookupError:
            await _NOT_FOUND(scope, receive, send)
            return

        state = request.app.state
        current_principal.set(principal)
        current_trace.set(parse_traceparent(request.headers.get("traceparent")))
        handler = _handler_for(state, principal, participant, agent, _base(request, handle))
        sdk_app = Starlette(
            routes=create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True)
        )
        inner: dict[str, Any] = {**scope, "path": "/", "raw_path": b"/", "root_path": ""}
        await sdk_app(inner, receive, send)


def _handler_for(
    state: Any, principal: Principal, callee: Participant, agent: Agent, rpc_url: str
) -> DefaultRequestHandlerV2:
    """One handler per (caller, callee): its task store (Postgres) is private to the caller, so an
    agent can never read another's tasks by guessing an id, and its running tasks outlive one
    request."""
    key = (principal.participant_id, callee.id)
    handlers: dict[tuple[uuid.UUID, uuid.UUID], DefaultRequestHandlerV2] = state.facade_handlers
    if key not in handlers:
        handlers[key] = DefaultRequestHandlerV2(
            agent_executor=facade.FacadeExecutor(state, callee.id),
            task_store=facade_store.PgTaskStore(
                state.sessionmaker, principal.participant_id, callee.id
            ),
            agent_card=facade.card_pb(facade.card_v1(callee, agent, rpc_url)),
            push_config_store=state.push_configs,
            push_sender=state.push_sender,
            push_url_validator=state.push_url_validator,
        )
    return handlers[key]


def routes() -> list[Route]:
    return [
        Route("/a2a/{handle}/.well-known/agent-card.json", _card, methods=["GET"]),
        Route("/a2a/{handle}/.well-known/agent.json", _card, methods=["GET"]),
        Route("/a2a/{handle}", RpcEndpoint(), methods=["POST"]),
    ]
