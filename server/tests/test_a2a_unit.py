"""Pure/unit tests for the A2A layer: versions, mapping, client wire handling, SSRF, secrets,
and the task state machine."""

import json
import uuid
from typing import Any

import httpx
import pytest

from app.a2a import mapping, versions
from app.a2a.client import A2AClient, A2AError, AgentEndpoint, auth_headers
from app.a2a.ssrf import UnsafeUrlError, validate_agent_url
from app.a2a.types import (
    A2AVersion,
    ArtifactEvent,
    MessageEvent,
    StatusEvent,
    TaskSnapshot,
)
from app.core import secrets
from app.core.lifecycle import InvalidTransitionError, TaskState, next_task_state

V1_CARD: dict[str, Any] = {
    "name": "Risk",
    "description": "scores risk",
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": "http://risk.test/grpc", "protocolBinding": "GRPC", "protocolVersion": "1.0"},
        {"url": "http://risk.test/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": True},
    "skills": [{"id": "risk.score", "name": "Score", "description": "d", "tags": ["risk"]}],
    "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
}
V03_CARD: dict[str, Any] = {
    "protocolVersion": "0.3.0",
    "name": "Legacy",
    "url": "http://old.test/rpc",
    "preferredTransport": "JSONRPC",
    "capabilities": {"streaming": False},
    "skills": [{"id": "a.b", "name": "AB", "tags": []}],
    "securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-Key"}},
}


# ---------------------------------------------------------------- cards


def test_parse_v1_card_picks_jsonrpc_interface() -> None:
    info = versions.parse_card(V1_CARD)
    assert info.version is A2AVersion.V1
    assert info.rpc_url == "http://risk.test/rpc"
    assert info.streaming and [s.id for s in info.skills] == ["risk.score"]
    assert info.security[0].kind == "bearer"


def test_parse_v03_card() -> None:
    info = versions.parse_card(V03_CARD)
    assert info.version is A2AVersion.V03
    assert info.rpc_url == "http://old.test/rpc"
    assert not info.streaming
    assert (info.security[0].kind, info.security[0].header) == ("api_key", "X-Key")
    assert info.protocol_versions == ("0.3.0",)


def test_v1_card_advertising_old_interface_version_uses_03_wire() -> None:
    card = {
        **V1_CARD,
        "supportedInterfaces": [
            {"url": "http://x.test/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"}
        ],
    }
    assert versions.detect_version(card) is A2AVersion.V03


def test_v03_additional_interfaces_and_rejections() -> None:
    card = {
        **V03_CARD,
        "preferredTransport": "GRPC",
        "additionalInterfaces": [{"url": "http://old.test/j", "transport": "JSONRPC"}],
    }
    assert versions.parse_card(card).rpc_url == "http://old.test/j"
    with pytest.raises(versions.CardError):
        versions.parse_card({**V03_CARD, "preferredTransport": "GRPC"})  # no JSON-RPC at all
    with pytest.raises(versions.CardError):
        versions.parse_card({"skills": []})  # no name
    with pytest.raises(versions.CardError):
        versions.parse_card(
            {
                **V1_CARD,
                "supportedInterfaces": [
                    {"url": "http://x", "protocolBinding": "GRPC", "protocolVersion": "1.0"}
                ],
            }
        )
    with pytest.raises(versions.CardError):
        versions.parse_card({**V03_CARD, "url": "ftp://old.test/rpc"})


# ---------------------------------------------------------------- wire


def test_states_roundtrip_both_dialects() -> None:
    for state in (
        "submitted",
        "working",
        "input_required",
        "auth_required",
        "completed",
        "failed",
        "canceled",
        "rejected",
    ):
        assert versions.state_from_wire(versions.state_to_wire(A2AVersion.V1, state)) == state
        assert versions.state_from_wire(versions.state_to_wire(A2AVersion.V03, state)) == state
    assert versions.state_to_wire(A2AVersion.V1, "input_required") == "TASK_STATE_INPUT_REQUIRED"
    assert versions.state_to_wire(A2AVersion.V03, "input_required") == "input-required"
    for bad in ("TASK_STATE_UNSPECIFIED", "unknown", "", None, 3):
        with pytest.raises(versions.WireError):
            versions.state_from_wire(bad)


def test_encode_message_per_version() -> None:
    parts = [{"text": "hi"}, {"data": {"a": 1}}]
    v1 = versions.encode_message(A2AVersion.V1, message_id="m", context_id="c", parts=parts)
    assert v1 == {
        "messageId": "m",
        "contextId": "c",
        "role": "ROLE_USER",
        "parts": [{"text": "hi"}, {"data": {"a": 1}}],
    }
    v3 = versions.encode_message(
        A2AVersion.V03, message_id="m", context_id="c", parts=parts, metadata={"k": 1}
    )
    assert v3["kind"] == "message" and v3["role"] == "user" and v3["metadata"] == {"k": 1}
    assert v3["parts"] == [{"kind": "text", "text": "hi"}, {"kind": "data", "data": {"a": 1}}]
    assert versions.method_name(A2AVersion.V1, "stream") == "SendStreamingMessage"
    assert versions.method_name(A2AVersion.V03, "stream") == "message/stream"
    assert versions.request_headers(A2AVersion.V1) == {"A2A-Version": "1.0"}
    assert versions.request_headers(A2AVersion.V03) == {}


def test_decode_v1_events() -> None:
    task = versions.decode_result(
        A2AVersion.V1,
        {
            "task": {
                "id": "T",
                "contextId": "C",
                "status": {"state": "TASK_STATE_SUBMITTED"},
                "artifacts": [{"artifactId": "A", "name": "r", "parts": [{"text": "x"}]}],
            }
        },
    )
    assert isinstance(task, TaskSnapshot) and task.state == "submitted"
    assert task.artifacts[0].parts == ({"text": "x"},)
    st = versions.decode_result(
        A2AVersion.V1,
        {
            "statusUpdate": {
                "taskId": "T",
                "contextId": "C",
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {"parts": [{"text": "which?"}]},
                },
            }
        },
    )
    assert st == StatusEvent("T", "C", "input_required", "which?")
    art = versions.decode_result(
        A2AVersion.V1,
        {
            "artifactUpdate": {
                "taskId": "T",
                "contextId": "C",
                "lastChunk": True,
                "artifact": {"artifactId": "A", "parts": [{"text": "x"}]},
            }
        },
    )
    assert isinstance(art, ArtifactEvent) and art.last_chunk and not art.append
    msg = versions.decode_result(A2AVersion.V1, {"message": {"parts": [{"text": "hello"}]}})
    assert isinstance(msg, MessageEvent)


def test_decode_v03_events() -> None:
    st = versions.decode_result(
        A2AVersion.V03,
        {
            "kind": "status-update",
            "taskId": "T",
            "contextId": "C",
            "final": True,
            "status": {
                "state": "input-required",
                "message": {"parts": [{"kind": "text", "text": "which?"}]},
            },
        },
    )
    assert st == StatusEvent("T", "C", "input_required", "which?")
    art = versions.decode_result(
        A2AVersion.V03,
        {
            "kind": "artifact-update",
            "taskId": "T",
            "contextId": "C",
            "append": True,
            "artifact": {
                "artifactId": "A",
                "parts": [
                    {"kind": "text", "text": "x"},
                    {"kind": "file", "file": {"uri": "http://f", "name": "f.txt"}},
                ],
            },
        },
    )
    assert isinstance(art, ArtifactEvent) and art.append and not art.last_chunk
    assert art.parts == ({"text": "x"}, {"url": "http://f", "filename": "f.txt"})
    for bad in ({"kind": "wat"}, "nope", {"kind": "task", "status": {"state": "working"}}):
        with pytest.raises(versions.WireError):
            versions.decode_result(A2AVersion.V03, bad)
    with pytest.raises(versions.WireError):
        versions.decode_result(A2AVersion.V1, {"unknown": {}})


# ---------------------------------------------------------------- mapping


def test_mapping_body_to_parts_and_ids() -> None:
    assert mapping.parts_from_body({"text": "hi"}) == [{"text": "hi"}]
    assert mapping.parts_from_body({"text": "hi", "data": {"n": 1}}) == [
        {"text": "hi"},
        {"data": {"n": 1}},
    ]
    assert mapping.parts_from_body({"parts": [{"url": "http://x"}]}) == [{"url": "http://x"}]
    assert mapping.parts_from_body({"claim": 7, "skill": "s"}) == [{"data": {"claim": 7}}]
    tid, rid = uuid.uuid4(), uuid.uuid4()
    assert mapping.message_id_for_task(tid) == mapping.message_id_for_task(tid)  # stable
    assert mapping.message_id_for_task(tid) != mapping.message_id_for_task(uuid.uuid4())
    assert mapping.context_id_for_room(rid) == str(rid)


# ---------------------------------------------------------------- client wire handling


def _client(handler: Any) -> A2AClient:
    return A2AClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _ep(version: A2AVersion = A2AVersion.V1, streaming: bool = True, **kw: Any) -> AgentEndpoint:
    return AgentEndpoint("http://a.test/rpc", version, streaming, 5.0, **kw)


async def _collect(c: A2AClient, ep: AgentEndpoint) -> list[Any]:
    return [
        e
        async for e in c.stream_message(
            ep, message_id="m", context_id="c", parts=[{"text": "hi"}], traceparent="00-aa-bb-01"
        )
    ]


async def test_client_non_streaming_send_returns_final_task() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"], seen["headers"] = json.loads(req.content), req.headers
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "m",
                "result": {
                    "task": {
                        "id": "T",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [{"artifactId": "A", "parts": [{"text": "done"}]}],
                    }
                },
            },
        )

    events = await _collect(_client(handler), _ep(streaming=False))
    assert isinstance(events[0], TaskSnapshot) and events[0].state == "completed"
    assert seen["body"]["method"] == "SendMessage"
    assert (
        seen["headers"]["a2a-version"] == "1.0" and seen["headers"]["traceparent"] == "00-aa-bb-01"
    )


async def test_client_parses_sse_and_ignores_comments() -> None:
    sse = (
        ": keepalive\n\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"kind":"task","id":"T",'
        '"status":{"state":"submitted"}}}\n\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"kind":"status-update","taskId":"T","final":true,'
        '"status":{"state":"completed"}}}'  # no trailing blank line
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    events = await _collect(_client(handler), _ep(A2AVersion.V03))
    assert [type(e).__name__ for e in events] == ["TaskSnapshot", "StatusEvent"]


async def test_client_error_classification() -> None:
    def status(code: int) -> Any:
        return lambda _r: httpx.Response(code, text="nope")

    for code, retryable, mismatch in [
        (503, True, False),
        (429, True, False),
        (401, False, False),
        (404, False, True),
    ]:
        with pytest.raises(A2AError) as e:
            await _collect(_client(status(code)), _ep())
        assert (e.value.retryable, e.value.version_mismatch) == (retryable, mismatch), code

    def rpc_error(code: int) -> Any:
        return lambda _r: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "x"}}
        )

    with pytest.raises(A2AError) as e:
        await _collect(_client(rpc_error(-32009)), _ep())
    assert e.value.version_mismatch and e.value.code == -32009
    with pytest.raises(A2AError) as e:
        await _collect(_client(rpc_error(-32001)), _ep())
    assert not e.value.version_mismatch

    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(A2AError) as e:
        await _collect(_client(boom), _ep())
    assert e.value.retryable

    garbage = lambda _r: httpx.Response(  # noqa: E731
        200, headers={"content-type": "text/event-stream"}, content="data: {not json\n\n"
    )
    with pytest.raises(A2AError):
        await _collect(_client(garbage), _ep())


def test_auth_headers_never_empty_or_partial() -> None:
    assert auth_headers({"type": "bearer", "token": "t"}) == {"Authorization": "Bearer t"}
    assert auth_headers({"type": "api_key", "header": "X-K", "value": "v"}) == {"X-K": "v"}
    assert auth_headers({"type": "bearer"}) == {}
    assert auth_headers(None) == {}


# ---------------------------------------------------------------- ssrf / secrets / lifecycle


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "http://0.0.0.0/x",
    ],
)
async def test_ssrf_blocks_non_public_hosts(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await validate_agent_url(url, allow_private=False)
    await validate_agent_url(url, allow_private=True)  # dev mode allows them


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.test/", "http://u:p@x.test/", "x"])
async def test_ssrf_rejects_bad_schemes_always(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await validate_agent_url(url, allow_private=True)


async def test_ssrf_allows_public_ip_literal() -> None:
    await validate_agent_url("https://8.8.8.8/card", allow_private=False)


def test_secrets_roundtrip_and_tamper() -> None:
    key = secrets.generate_key()
    wrapped = secrets.encrypt_json(key, {"type": "bearer", "token": "s3cret"})
    assert "s3cret" not in json.dumps(wrapped)
    assert secrets.decrypt_json(key, wrapped) == {"type": "bearer", "token": "s3cret"}
    with pytest.raises(secrets.DecryptionError):
        secrets.decrypt_json(secrets.generate_key(), wrapped)
    with pytest.raises(secrets.DecryptionError):
        secrets.decrypt_json(key, {"v": 1, "ct": "garbage"})
    with pytest.raises(secrets.DecryptionError):
        secrets.decrypt_json(key, {})


def test_task_state_machine() -> None:
    S, W, N, A, C, F, X, R = (
        TaskState.SUBMITTED,
        TaskState.WORKING,
        TaskState.INPUT_REQUIRED,
        TaskState.AUTH_REQUIRED,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.REJECTED,
    )
    assert next_task_state(S, W) == W and next_task_state(W, N) == N and next_task_state(N, W) == W
    assert next_task_state(W, A) == A and next_task_state(A, C) == C
    assert next_task_state(S, R) == R and next_task_state(W, X) == X
    assert next_task_state(C, C) == C  # idempotent
    for terminal in (C, F, X, R):
        for target in (W, S, N, C, F, X):
            if target != terminal:
                with pytest.raises(InvalidTransitionError):
                    next_task_state(terminal, target)
    for current in (W, N, A):
        with pytest.raises(InvalidTransitionError):
            next_task_state(current, S)  # nothing returns to submitted
