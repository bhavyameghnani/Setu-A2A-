import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from a2a.server.tasks import BasePushNotificationSender
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.a2a import facade_store, registry
from app.a2a.client import A2AClient
from app.a2a.dispatch import Dispatcher
from app.a2a.facade import current_principal
from app.a2a.ssrf import UnsafeUrlError, validate_agent_url
from app.api import (
    a2a_facade,
    agents,
    analytics,
    approvals,
    audit,
    auth,
    delegations,
    delivery,
    departments,
    messages,
    notifications,
    participants,
    policies,
    rooms,
    tasks,
    teams,
    traces,
)
from app.config import get_settings
from app.core.signing import signer_from_settings
from app.db.session import make_engine, make_sessionmaker
from app.db.snapshots import snapshot_loop, stop_task
from app.logging import configure_logging
from app.ws import gateway
from app.ws import notifications as ws_notifications
from app.ws.notify import Notifier


def create_app(
    database_url: str | None = None, a2a_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """`a2a_transport` lets tests route agent calls to in-process apps instead of the network."""
    settings = get_settings()
    configure_logging(settings.log_level)
    url = database_url or settings.database_url
    jwt_secret = settings.resolved_jwt_secret()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(url)
        notifier = Notifier(url)
        app.state.settings = settings
        app.state.jwt_secret = jwt_secret
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.notifier = notifier
        http = httpx.AsyncClient(
            transport=a2a_transport,
            follow_redirects=False,  # a redirect could bounce us to an internal address
            headers={"User-Agent": "Setu-A2A-Hub/0.1"},
        )
        client = A2AClient(http)
        dispatcher = Dispatcher(app.state.sessionmaker, client, settings)
        app.state.a2a_client = client
        app.state.facade_handlers = {}
        app.state.facade_cancelled = {}
        # Webhooks for facade tasks (A2A push notifications): configs in Postgres, delivery over
        # the same SSRF-guarded client as outbound agent calls.
        app.state.push_configs = facade_store.PgPushConfigStore(
            app.state.sessionmaker, settings.resolved_encryption_key(), current_principal
        )

        async def push_url_ok(url: str) -> bool:
            try:
                await validate_agent_url(url, allow_private=settings.allow_private_agent_hosts())
            except UnsafeUrlError:
                return False
            return True

        app.state.push_url_validator = push_url_ok
        app.state.push_sender = BasePushNotificationSender(
            http, app.state.push_configs, push_url_validator=push_url_ok
        )
        app.state.dispatcher = dispatcher
        app.state.signer = signer_from_settings(settings)
        refresh: asyncio.Task[None] | None = None
        if settings.agent_card_refresh_seconds > 0:
            refresh = asyncio.create_task(
                registry.refresh_loop(
                    app.state.sessionmaker,
                    client,
                    settings.agent_card_refresh_seconds,
                    allow_private=settings.allow_private_agent_hosts(),
                    encryption_key=settings.resolved_encryption_key(),
                )
            )
        dispatcher.start()
        snap: asyncio.Task[None] | None = None
        if settings.snapshot_interval_seconds > 0:
            snap = asyncio.create_task(
                snapshot_loop(
                    app.state.sessionmaker,
                    settings.snapshot_interval_seconds,
                    settings.snapshot_min_messages,
                )
            )
        notifier.start()  # connects in the background; Postgres being down does not block boot
        try:
            yield
        finally:
            await stop_task(snap)
            await registry.stop_task(refresh)
            await dispatcher.stop()
            await http.aclose()
            await notifier.stop()
            await engine.dispose()

    app = FastAPI(title="Setu", version="0.0.0", lifespan=lifespan)
    for r in (
        auth.router,
        agents.router,
        analytics.router,
        teams.router,
        tasks.router,
        delivery.router,
        departments.router,
        participants.router,
        rooms.router,
        messages.router,
        policies.router,
        delegations.router,
        approvals.router,
        audit.router,
        traces.router,
        notifications.router,
    ):
        app.include_router(r)
    app.include_router(gateway.router)
    app.include_router(ws_notifications.router)
    app.router.routes.extend(a2a_facade.routes())

    @app.get("/.well-known/jwks.json")
    async def jwks(request: Request) -> JSONResponse:
        """Setu's public signing key: how anyone verifies a signed artifact."""
        return JSONResponse(
            request.app.state.signer.jwks(), headers={"Cache-Control": "max-age=300"}
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        try:
            async with request.app.state.sessionmaker() as session:
                await session.execute(text("select 1"))
        except Exception:
            return JSONResponse({"status": "degraded", "database": "down"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "up"})

    return app


app = create_app()
