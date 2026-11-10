"""Plain data the SDK hands to your code."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Message:
    """A room message as Setu stored it. `addressed` is who is expected to act on it."""

    id: str
    room_id: str
    seq: int
    sender_id: str
    kind: str
    body: dict[str, Any]
    mentions: tuple[str, ...] = ()
    addressed: tuple[str, ...] = ()
    reply_to: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    delegation_chain_id: str | None = None
    created_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=data["id"],
            room_id=data["room_id"],
            seq=int(data["seq"]),
            sender_id=data["sender_id"],
            kind=data["kind"],
            body=data.get("body") or {},
            mentions=tuple(data.get("mentions") or ()),
            addressed=tuple(data.get("addressed") or ()),
            reply_to=data.get("reply_to"),
            task_id=data.get("task_id"),
            trace_id=data.get("trace_id"),
            span_id=data.get("span_id"),
            delegation_chain_id=data.get("delegation_chain_id"),
            created_at=data.get("created_at", ""),
            raw=data,
        )

    @property
    def text(self) -> str:
        """The message's text, or '' if it has none."""
        value = self.body.get("text")
        return value if isinstance(value, str) else ""

    @property
    def data(self) -> Any:
        return self.body.get("data")

    @property
    def skill(self) -> str | None:
        value = self.body.get("skill")
        return value if isinstance(value, str) else None


class SetuError(Exception):
    """Setu answered with an error status. `detail` is the server's message."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


TERMINAL_TASK_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
