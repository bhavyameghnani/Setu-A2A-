"""Outbound A2A calls to registered agents: thin JSON-RPC over HTTP with SSE streaming.

Plain httpx on purpose (see docs/a2a-notes.md section 6): per-agent version negotiation,
traceparent, card-derived auth headers and raw-frame control are easier here than through the
SDK client. Wire differences between A2A 1.0 and 0.3 live in `app.a2a.versions`.
"""

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.a2a import versions
from app.a2a.types import A2AVersion, Event, Part

MAX_CARD_BYTES = 1_000_000
MAX_EVENT_BYTES = 4_000_000


class A2AError(Exception):
    """A failed call to an agent. `retryable` is true for outages worth retrying;
    `version_mismatch` means the card is probably stale (refresh it and retry once)."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        retryable: bool = False,
        version_mismatch: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.version_mismatch = version_mismatch


@dataclass(frozen=True)
class AgentEndpoint:
    rpc_url: str
    version: A2AVersion
    streaming: bool
    timeout: float  # seconds of silence tolerated, per read
    headers: Mapping[str, str] = field(default_factory=dict)  # credentials; never logged


def auth_headers(auth: Mapping[str, Any] | None) -> dict[str, str]:
    """Build credential headers from Setu's stored auth config."""
    if not auth:
        return {}
    kind = auth.get("type")
    if kind == "bearer" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    if kind == "api_key" and auth.get("header") and auth.get("value"):
        return {str(auth["header"]): str(auth["value"])}
    return {}


class A2AClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    # ------------------------------------------------------------ cards

    async def fetch_card(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,  # noqa: ASYNC109
    ) -> dict[str, Any]:
        try:
            resp = await self._http.get(url, headers=dict(headers or {}), timeout=timeout)
        except httpx.HTTPError as exc:
            raise A2AError(f"cannot reach {url}: {exc.__class__.__name__}", retryable=True) from exc
        if resp.status_code != 200:
            raise A2AError(f"agent card request returned HTTP {resp.status_code}")
        if len(resp.content) > MAX_CARD_BYTES:
            raise A2AError("agent card is too large")
        try:
            card = resp.json()
        except ValueError as exc:
            raise A2AError("agent card is not valid JSON") from exc
        if not isinstance(card, dict):
            raise A2AError("agent card must be a JSON object")
        return card

    # ------------------------------------------------------------ calls

    def _headers(self, ep: AgentEndpoint, traceparent: str | None, sse: bool) -> dict[str, str]:
        h = {"Content-Type": "application/json", **versions.request_headers(ep.version)}
        h["Accept"] = "text/event-stream" if sse else "application/json"
        h.update(ep.headers)
        if traceparent:
            h["traceparent"] = traceparent
        return h

    def _timeout(self, ep: AgentEndpoint) -> httpx.Timeout:
        return httpx.Timeout(connect=10.0, read=ep.timeout, write=10.0, pool=10.0)

    async def stream_message(
        self,
        ep: AgentEndpoint,
        *,
        message_id: str,
        context_id: str,
        parts: list[Part],
        metadata: Mapping[str, Any] | None = None,
        traceparent: str | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """Send a message and yield normalized events until the agent finishes.

        Uses SSE when the card says the agent streams, otherwise a blocking send whose final
        Task is yielded as a single snapshot.
        """
        message = versions.encode_message(
            ep.version,
            message_id=message_id,
            context_id=context_id,
            parts=parts,
            metadata=metadata,
            task_id=task_id,  # continue an existing task (e.g. after input_required)
        )
        op = "stream" if ep.streaming else "send"
        payload = versions.encode_request(ep.version, op, message_id, {"message": message})
        headers = self._headers(ep, traceparent, sse=ep.streaming)
        try:
            async with self._http.stream(
                "POST", ep.rpc_url, json=payload, headers=headers, timeout=self._timeout(ep)
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:200].decode(errors="replace")
                    raise A2AError(
                        f"agent returned HTTP {resp.status_code}: {body}",
                        retryable=resp.status_code >= 500 or resp.status_code == 429,
                        version_mismatch=resp.status_code in (404, 405, 415),
                    )
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    async for data in self._sse_data(resp):
                        yield self._decode(ep.version, data)
                else:  # blocking reply (or a JSON-RPC error with a 200)
                    raw = await resp.aread()
                    if len(raw) > MAX_EVENT_BYTES:
                        raise A2AError("agent response is too large")
                    yield self._decode(ep.version, raw.decode())
        except httpx.TimeoutException as exc:
            raise A2AError("agent timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise A2AError(f"transport error: {exc.__class__.__name__}", retryable=True) from exc

    async def _sse_data(self, resp: httpx.Response) -> AsyncIterator[str]:
        buf: list[str] = []
        size = 0
        async for line in resp.aiter_lines():
            if line == "":
                if buf:
                    yield "\n".join(buf)
                    buf, size = [], 0
                continue
            if line.startswith(":"):  # comment / keep-alive
                continue
            if line.startswith("data:"):
                chunk = line[5:].removeprefix(" ")
                size += len(chunk)
                if size > MAX_EVENT_BYTES:
                    raise A2AError("agent event is too large")
                buf.append(chunk)
        if buf:  # stream ended without a trailing blank line
            yield "\n".join(buf)

    def _decode(self, version: A2AVersion, data: str) -> Event:
        try:
            frame = json.loads(data)
        except ValueError as exc:
            raise A2AError("agent sent invalid JSON") from exc
        if not isinstance(frame, dict):
            raise A2AError("agent sent a non-object frame")
        err = frame.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise A2AError(
                f"agent error {code}: {msg}",
                code=code if isinstance(code, int) else None,
                version_mismatch=code in versions.VERSION_MISMATCH_CODES,
            )
        try:
            return versions.decode_result(version, frame.get("result"))
        except versions.WireError as exc:
            raise A2AError(f"invalid {version.value} response: {exc}") from exc

    async def cancel_task(
        self, ep: AgentEndpoint, a2a_task_id: str, traceparent: str | None = None
    ) -> None:
        payload = versions.encode_request(ep.version, "cancel", 1, {"id": a2a_task_id})
        try:
            resp = await self._http.post(
                ep.rpc_url,
                json=payload,
                headers=self._headers(ep, traceparent, sse=False),
                timeout=self._timeout(ep),
            )
        except httpx.HTTPError as exc:
            raise A2AError(f"cancel failed: {exc.__class__.__name__}", retryable=True) from exc
        if resp.status_code >= 400:
            raise A2AError(f"cancel returned HTTP {resp.status_code}")
        try:
            frame = resp.json()
        except ValueError as exc:
            raise A2AError("cancel response is not JSON") from exc
        if isinstance(frame, dict) and frame.get("error"):
            err = frame["error"]
            raise A2AError(
                f"cancel refused: {err.get('message') if isinstance(err, dict) else err}"
            )
