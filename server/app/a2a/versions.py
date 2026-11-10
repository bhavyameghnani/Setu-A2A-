"""A2A 1.0 and 0.3 compatibility: card detection plus per-version wire encode/decode.

Everything above this module speaks the neutral model in `app.a2a.types`. Version differences
(method names, enum spelling, part shapes, event wrapping, the A2A-Version header) are confined
here. Verified against a2a-sdk 1.3.0 and the 0.3 wire shapes; see docs/a2a-notes.md.
"""

import base64
from collections.abc import Mapping
from typing import Any

from app.a2a.types import (
    A2AVersion,
    ArtifactEvent,
    CardInfo,
    Event,
    MessageEvent,
    Part,
    SecurityScheme,
    Skill,
    StatusEvent,
    TaskSnapshot,
)

CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
VERSION_HEADER = "A2A-Version"
# JSON-RPC errors that mean "you are speaking the wrong dialect": refresh the card and retry.
VERSION_MISMATCH_CODES = frozenset({-32009, -32601})

# Setu state names (underscored) <-> wire spellings.
SETU_STATES = frozenset(
    {
        "submitted",
        "working",
        "input_required",
        "auth_required",
        "completed",
        "failed",
        "canceled",
        "rejected",
    }
)


class CardError(ValueError):
    """The Agent Card is unusable for Setu."""


class WireError(ValueError):
    """The agent sent something that is not valid A2A for the negotiated version."""


# ---------------------------------------------------------------- cards


def _str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def detect_version(card: Mapping[str, Any]) -> A2AVersion:
    return _select_interface(card)[1]


def _select_interface(card: Mapping[str, Any]) -> tuple[str, A2AVersion, str]:
    """(rpc_url, wire version, declared protocol version) for the JSON-RPC interface."""
    ifaces = card.get("supportedInterfaces")
    if isinstance(ifaces, list) and ifaces:  # 1.0 card shape
        candidates = [i for i in ifaces if isinstance(i, dict)]
        rpc = [i for i in candidates if _str(i.get("protocolBinding")).upper() == "JSONRPC"]
        if not rpc:
            raise CardError("agent offers no JSON-RPC interface (only gRPC/REST are advertised)")
        chosen = max(rpc, key=lambda i: _major_minor(_str(i.get("protocolVersion"), "1.0")))
        declared = _str(chosen.get("protocolVersion"), "1.0")
        url = _str(chosen.get("url"))
    else:  # legacy 0.3 card shape: top-level url
        declared = _str(card.get("protocolVersion"), "0.3.0")
        transport = _str(card.get("preferredTransport"), "JSONRPC").upper()
        url = _str(card.get("url")) if transport == "JSONRPC" else ""
        if not url:
            for extra in card.get("additionalInterfaces") or []:
                if isinstance(extra, dict) and _str(extra.get("transport")).upper() == "JSONRPC":
                    url = _str(extra.get("url"))
                    break
        if not url:
            raise CardError("agent card has no JSON-RPC url")
    if not url:
        raise CardError("agent card interface has no url")
    version = A2AVersion.V03 if declared.startswith("0.") else A2AVersion.V1
    return url, version, declared


def _major_minor(v: str) -> tuple[int, int]:
    parts = v.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return (0, 0)


def _parse_security(card: Mapping[str, Any]) -> tuple[SecurityScheme, ...]:
    out: list[SecurityScheme] = []
    schemes = card.get("securitySchemes")
    if not isinstance(schemes, dict):
        return ()
    for name, raw in schemes.items():
        if not isinstance(raw, dict):
            continue
        if "httpAuthSecurityScheme" in raw:  # 1.0
            s = raw["httpAuthSecurityScheme"] or {}
            kind = "bearer" if _str(s.get("scheme")).lower() == "bearer" else "other"
            out.append(SecurityScheme(name, kind))
        elif "apiKeySecurityScheme" in raw:  # 1.0
            s = raw["apiKeySecurityScheme"] or {}
            where = _str(s.get("location")).lower()
            out.append(
                SecurityScheme(
                    name, "api_key" if where == "header" else "other", _str(s.get("name")) or None
                )
            )
        elif raw.get("type") == "http":  # 0.3
            kind = "bearer" if _str(raw.get("scheme")).lower() == "bearer" else "other"
            out.append(SecurityScheme(name, kind))
        elif raw.get("type") == "apiKey":  # 0.3
            header = _str(raw.get("name")) or None
            out.append(
                SecurityScheme(name, "api_key" if raw.get("in") == "header" else "other", header)
            )
        else:
            out.append(SecurityScheme(name, "other"))
    return tuple(out)


def parse_card(card: Mapping[str, Any]) -> CardInfo:
    if not isinstance(card, dict):
        raise CardError("agent card must be a JSON object")
    name = _str(card.get("name")).strip()
    if not name:
        raise CardError("agent card has no name")
    rpc_url, version, declared = _select_interface(card)
    if not rpc_url.startswith(("http://", "https://")):
        raise CardError("agent card url must be http(s)")
    skills: list[Skill] = []
    for s in card.get("skills") or []:
        if isinstance(s, dict) and _str(s.get("id")):
            tags = tuple(t for t in (s.get("tags") or []) if isinstance(t, str))
            skills.append(
                Skill(
                    _str(s["id"]),
                    _str(s.get("name"), _str(s["id"])),
                    _str(s.get("description")),
                    tags,
                )
            )
    caps = card.get("capabilities")
    streaming = bool(caps.get("streaming")) if isinstance(caps, dict) else False
    declared_versions = {declared}
    for i in card.get("supportedInterfaces") or []:
        if isinstance(i, dict) and _str(i.get("protocolVersion")):
            declared_versions.add(_str(i["protocolVersion"]))
    return CardInfo(
        name=name,
        description=_str(card.get("description")),
        version=version,
        rpc_url=rpc_url,
        streaming=streaming,
        skills=tuple(skills),
        security=_parse_security(card),
        protocol_versions=tuple(sorted(declared_versions)),
    )


# ---------------------------------------------------------------- states


def state_from_wire(raw: Any) -> str:
    """'TASK_STATE_INPUT_REQUIRED' (1.0) and 'input-required' (0.3) both become 'input_required'."""
    if not isinstance(raw, str) or not raw:
        raise WireError("task status has no state")
    s = raw.lower().removeprefix("task_state_").replace("-", "_")
    if s not in SETU_STATES:
        raise WireError(f"unsupported task state {raw!r}")
    return s


def state_to_wire(version: A2AVersion, state: str) -> str:
    if version is A2AVersion.V1:
        return "TASK_STATE_" + state.upper()
    return state.replace("_", "-")


# ---------------------------------------------------------------- parts


def parts_from_wire(raw: Any) -> tuple[Part, ...]:
    out: list[Part] = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        if kind == "text":  # 0.3
            out.append({"text": _str(p.get("text"))})
        elif kind == "data":
            out.append({"data": p.get("data")})
        elif kind == "file":
            f = p.get("file") or {}
            part: Part = {}
            if "uri" in f:
                part["url"] = f["uri"]
            if "bytes" in f:
                part["raw"] = f["bytes"]
            if f.get("name"):
                part["filename"] = f["name"]
            if f.get("mimeType"):
                part["mediaType"] = f["mimeType"]
            out.append(part)
        else:  # 1.0 parts have no discriminator
            out.append({k: v for k, v in p.items() if k != "kind"})
    return tuple(out)


def parts_to_wire(version: A2AVersion, parts: list[Part]) -> list[dict[str, Any]]:
    if version is A2AVersion.V1:
        return [dict(p) for p in parts]
    out: list[dict[str, Any]] = []
    for p in parts:
        if "text" in p:
            out.append({"kind": "text", "text": p["text"]})
        elif "data" in p:
            out.append({"kind": "data", "data": p["data"]})
        elif "url" in p or "raw" in p:
            f: dict[str, Any] = {}
            if "url" in p:
                f["uri"] = p["url"]
            if "raw" in p:
                f["bytes"] = (
                    p["raw"] if isinstance(p["raw"], str) else base64.b64encode(p["raw"]).decode()
                )
            if p.get("filename"):
                f["name"] = p["filename"]
            if p.get("mediaType"):
                f["mimeType"] = p["mediaType"]
            out.append({"kind": "file", "file": f})
    return out


# ---------------------------------------------------------------- requests


def request_headers(version: A2AVersion) -> dict[str, str]:
    return {VERSION_HEADER: "1.0"} if version is A2AVersion.V1 else {}


def method_name(version: A2AVersion, op: str) -> str:
    table = {
        "send": ("SendMessage", "message/send"),
        "stream": ("SendStreamingMessage", "message/stream"),
        "get": ("GetTask", "tasks/get"),
        "cancel": ("CancelTask", "tasks/cancel"),
    }
    return table[op][0 if version is A2AVersion.V1 else 1]


def encode_message(
    version: A2AVersion,
    *,
    message_id: str,
    context_id: str,
    parts: list[Part],
    metadata: Mapping[str, Any] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "messageId": message_id,
        "contextId": context_id,
        "role": "ROLE_USER" if version is A2AVersion.V1 else "user",
        "parts": parts_to_wire(version, parts),
    }
    if version is A2AVersion.V03:
        msg["kind"] = "message"
    if task_id:
        msg["taskId"] = task_id
    if metadata:
        msg["metadata"] = dict(metadata)
    return msg


def encode_request(
    version: A2AVersion, op: str, request_id: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method_name(version, op),
        "params": params,
    }


# ---------------------------------------------------------------- responses


def _status_text(status: Any) -> str | None:
    if not isinstance(status, dict):
        return None
    msg = status.get("message")
    if not isinstance(msg, dict):
        return None
    texts = [p["text"] for p in parts_from_wire(msg.get("parts")) if isinstance(p.get("text"), str)]
    return "\n".join(texts) or None


def _artifact(
    raw: dict[str, Any], task_id: str | None, context_id: str | None, last_chunk: bool, append: bool
) -> ArtifactEvent:
    art_id = _str(raw.get("artifactId"))
    if not art_id:
        raise WireError("artifact has no artifactId")
    return ArtifactEvent(
        task_id=task_id,
        context_id=context_id,
        artifact_id=art_id,
        name=_str(raw.get("name")) or None,
        parts=parts_from_wire(raw.get("parts")),
        last_chunk=last_chunk,
        append=append,
    )


def _task(raw: dict[str, Any]) -> TaskSnapshot:
    task_id = _str(raw.get("id"))
    if not task_id:
        raise WireError("task has no id")
    ctx = _str(raw.get("contextId")) or None
    status = raw.get("status")
    if not isinstance(status, dict):
        raise WireError("task has no status")
    arts = tuple(
        _artifact(a, task_id, ctx, True, False)
        for a in raw.get("artifacts") or []
        if isinstance(a, dict)
    )
    return TaskSnapshot(
        task_id, ctx, state_from_wire(status.get("state")), _status_text(status), arts
    )


def decode_result(version: A2AVersion, result: Any) -> Event:
    """Decode one JSON-RPC `result` (a stream frame or a non-streaming response)."""
    if not isinstance(result, dict):
        raise WireError("result is not an object")
    if version is A2AVersion.V1:
        if "task" in result:
            return _task(result["task"])
        if "statusUpdate" in result:
            u = result["statusUpdate"]
            return StatusEvent(
                _str(u.get("taskId")) or None,
                _str(u.get("contextId")) or None,
                state_from_wire((u.get("status") or {}).get("state")),
                _status_text(u.get("status")),
            )
        if "artifactUpdate" in result:
            u = result["artifactUpdate"]
            return _artifact(
                u.get("artifact") or {},
                _str(u.get("taskId")) or None,
                _str(u.get("contextId")) or None,
                bool(u.get("lastChunk")),
                bool(u.get("append")),
            )
        if "message" in result:
            m = result["message"]
            return MessageEvent(_str(m.get("contextId")) or None, parts_from_wire(m.get("parts")))
        raise WireError("unrecognized 1.0 result")
    kind = result.get("kind")
    if kind == "task":
        return _task(result)
    if kind == "status-update":
        return StatusEvent(
            _str(result.get("taskId")) or None,
            _str(result.get("contextId")) or None,
            state_from_wire((result.get("status") or {}).get("state")),
            _status_text(result.get("status")),
        )
    if kind == "artifact-update":
        return _artifact(
            result.get("artifact") or {},
            _str(result.get("taskId")) or None,
            _str(result.get("contextId")) or None,
            bool(result.get("lastChunk")),
            bool(result.get("append")),
        )
    if kind == "message":
        return MessageEvent(
            _str(result.get("contextId")) or None, parts_from_wire(result.get("parts"))
        )
    raise WireError(f"unrecognized 0.3 result kind {kind!r}")
