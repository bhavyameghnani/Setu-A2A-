"""Setu <-> A2A translation (spec section 8.3)."""

import re
import uuid
from typing import Any

from app.a2a.types import ArtifactEvent, Part

_URL = re.compile(r"https?://\S+")


def scrub(text: str) -> str:
    """Nothing shown to other participants may contain an address: an agent's real endpoint
    stays between Setu and that agent (spec 8.5)."""
    return _URL.sub("[address hidden]", text)


# Stable namespace so a retry of the same Setu task reuses the same A2A messageId (idempotency).
_NS = uuid.UUID("5e7d0000-0000-4000-8000-5e7d00000001")


def message_id_for_task(task_id: uuid.UUID) -> str:
    return str(uuid.uuid5(_NS, str(task_id)))


def context_id_for_room(room_id: uuid.UUID) -> str:
    """One room = one A2A context. The room id itself is already stable and unique."""
    return str(room_id)


def parts_from_body(body: dict[str, Any]) -> list[Part]:
    """Setu `task` message body -> A2A parts. Body: {"text": str, "data": any, "parts": [...]}.

    An explicit `parts` list (already in the neutral shape) wins; otherwise text and data
    are turned into parts. A body with neither still produces a data part, never an empty message.
    """
    explicit = body.get("parts")
    if isinstance(explicit, list) and explicit:
        return [p for p in explicit if isinstance(p, dict)]
    parts: list[Part] = []
    if isinstance(body.get("text"), str) and body["text"]:
        parts.append({"text": body["text"]})
    if body.get("data") is not None:
        parts.append({"data": body["data"]})
    if not parts:
        parts.append({"data": {k: v for k, v in body.items() if k not in ("skill", "skills")}})
    return parts


def status_body(
    task_id: uuid.UUID,
    state: str,
    text: str | None,
    *,
    origin: str = "agent",
    error: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"task_id": str(task_id), "state": state, "origin": origin}
    if text:
        body["text"] = text
    if error:
        body["error"] = error
    return body


def artifact_body(task_id: uuid.UUID, art: ArtifactEvent, parts: list[Part]) -> dict[str, Any]:
    texts = [p["text"] for p in parts if isinstance(p.get("text"), str)]
    body: dict[str, Any] = {
        "task_id": str(task_id),
        "artifact_id": art.artifact_id,
        "name": art.name,
        "parts": parts,
    }
    if texts:
        body["text"] = "\n".join(texts)
    return body
