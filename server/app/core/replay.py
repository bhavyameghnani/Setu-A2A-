"""Cursor and snapshot rules. Pure; the database work lives in app.db.delivery / snapshots."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass


def resume_seq(requested: int | None, cursor: int | None) -> int:
    """Where a (re)connecting client starts. An explicit `after_seq` wins; otherwise resume from
    the stored cursor; otherwise from the beginning."""
    if requested is not None:
        return max(requested, 0)
    return max(cursor or 0, 0)


def advance_cursor(current: int, proposed: int, room_last_seq: int) -> int:
    """Cursors only move forward (a late duplicate ack must not rewind) and never past the end."""
    if proposed < 0:
        raise ValueError("seq must be >= 0")
    if proposed > room_last_seq:
        raise ValueError(f"seq {proposed} is beyond the room's last seq {room_last_seq}")
    return max(current, proposed)


@dataclass(frozen=True)
class SnapshotItem:
    seq: int
    kind: str
    sender_handle: str


def summarize_segment(items: Sequence[SnapshotItem]) -> str:
    """Deterministic digest of a run of messages: counts by kind and by sender.

    This is the default summarizer. An LLM-written summary may replace it later, but only in a
    background job, never in the message path (spec section 12).
    """
    if not items:
        return "No messages."
    lo, hi = items[0].seq, items[-1].seq
    kinds = Counter(i.kind for i in items)
    senders = Counter(i.sender_handle for i in items)
    kind_part = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    sender_part = ", ".join(f"{h} ({n})" for h, n in sorted(senders.items()))
    return f"Messages {lo}-{hi} ({len(items)}): {kind_part}. From: {sender_part}."
