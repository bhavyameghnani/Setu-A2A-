"""Integration fixtures: a throwaway Postgres database per test session (no mocks, no Docker)."""

import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

# The demo lives outside the server package; make `cross_department` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples"))

# Fast retries in tests; must be set before app.config.get_settings() is first called.
os.environ.setdefault("A2A_RETRY_BACKOFF_SECONDS", "0.01")
os.environ.setdefault("SNAPSHOT_INTERVAL_SECONDS", "0")
os.environ.setdefault("AGENT_CARD_REFRESH_SECONDS", "0")

from app.config import get_settings  # noqa: E402
from app.db.session import make_engine


async def _admin_connect(url: str) -> asyncpg.Connection:
    u = make_url(url)
    return await asyncpg.connect(
        user=u.username,
        password=u.password,
        host=u.host,
        port=u.port,
        database="postgres",
    )


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Create setu_test_<id>, point DATABASE_URL at it, drop it at session end."""
    import asyncio

    base = get_settings().database_url
    name = f"setu_test_{uuid.uuid4().hex[:8]}"

    async def create() -> None:
        conn = await _admin_connect(base)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    async def drop() -> None:
        conn = await _admin_connect(base)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()

    asyncio.run(create())
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(drop())


@pytest.fixture(scope="session")
def migrated_database_url(test_database_url: str) -> str:
    """Throwaway database with the real Alembic migrations applied (no create_all shortcut)."""
    from pathlib import Path

    from alembic.config import Config

    from alembic import command

    server_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(server_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(server_dir / "alembic"))
    command.upgrade(cfg, "head")  # env.py reads DATABASE_URL, set by test_database_url
    return test_database_url


@pytest_asyncio.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(test_database_url)
    try:
        yield eng
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------- live servers
# The SDK talks HTTP and WebSocket, so its tests (and the demo's) need a real listening hub.


class LiveHub:
    """The real app on a real port, served by uvicorn in a background thread."""

    def __init__(self, database_url: str) -> None:
        import threading

        import uvicorn
        from setu_sdk.a2a_serve import free_port

        from app.main import create_app

        self.database_url = database_url
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                create_app(database_url), host="127.0.0.1", port=self.port, log_level="warning"
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        import time

        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("live hub did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


@pytest.fixture(scope="module")
def live_hub(migrated_database_url: str) -> Iterator[LiveHub]:
    hub = LiveHub(migrated_database_url)
    hub.start()
    try:
        yield hub
    finally:
        hub.stop()
