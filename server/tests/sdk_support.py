"""Helpers for tests that run the SDK, A2A servers and the hub as real network servers."""

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from setu_sdk.a2a_serve import free_port

from tests.helpers import Acme


class Served:
    """An ASGI app served on a real port in a background thread."""

    def __init__(self, app: FastAPI, port: int | None = None) -> None:
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "Served":
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def live_acme(hub_url: str, database_url: str) -> Acme:
    """The usual Acme world builder, talking to a live hub over HTTP."""
    return Acme(httpx.Client(base_url=hub_url, timeout=30), database_url)  # type: ignore[arg-type]


class Proxy:
    """A TCP forwarder that can cut every open connection (to test reconnects)."""

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.port = free_port()
        self._conns: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", self.port)

    async def stop(self) -> None:
        self.drop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def drop(self) -> None:
        for w in list(self._conns):
            w.close()
        self._conns.clear()

    async def _on_client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            w.close()
            return
        self._conns.update({w, uw})

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.close()

        await asyncio.gather(pipe(r, uw), pipe(ur, w))


async def until(fn: Callable[[], Any], within: float = 15.0, what: str = "condition") -> Any:
    """Await a (sync or async) check until it returns something truthy."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        got = fn()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@contextlib.asynccontextmanager
async def running(agent: Any) -> AsyncIterator[asyncio.Task[None]]:
    """Run an SDK agent as a background task; cancel it (and wait for it) on exit."""
    task = asyncio.get_running_loop().create_task(agent.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
