"""Version-neutral A2A model. Both wire versions are translated to and from these."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class A2AVersion(StrEnum):
    V1 = "1.0"
    V03 = "0.3"


# A normalized part follows the 1.0 shape: {"text": str} | {"data": Any} | {"url": str, ...}
# | {"raw": base64 str, ...}. The 0.3 adapter converts to and from {"kind": ...} parts.
Part = dict[str, Any]


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityScheme:
    name: str
    kind: str  # 'bearer' | 'api_key' | 'other'
    header: str | None = None  # header name for api_key


@dataclass(frozen=True)
class CardInfo:
    name: str
    description: str
    version: A2AVersion  # wire version to speak to this agent
    rpc_url: str
    streaming: bool
    skills: tuple[Skill, ...]
    security: tuple[SecurityScheme, ...]
    protocol_versions: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactEvent:
    task_id: str | None
    context_id: str | None
    artifact_id: str
    name: str | None
    parts: tuple[Part, ...]
    last_chunk: bool = False
    append: bool = False


@dataclass(frozen=True)
class StatusEvent:
    task_id: str | None
    context_id: str | None
    state: str  # a Setu task state, e.g. 'input_required'
    text: str | None = None


@dataclass(frozen=True)
class TaskSnapshot:
    """A whole Task object: the first stream event, or the result of a non-streaming send."""

    task_id: str
    context_id: str | None
    state: str
    text: str | None = None
    artifacts: tuple[ArtifactEvent, ...] = ()


@dataclass(frozen=True)
class MessageEvent:
    """The agent replied with a plain Message instead of starting a Task."""

    context_id: str | None
    parts: tuple[Part, ...]


Event = TaskSnapshot | StatusEvent | ArtifactEvent | MessageEvent


def text_of(parts: Sequence[Part]) -> str:
    return "\n".join(p["text"] for p in parts if isinstance(p.get("text"), str))
