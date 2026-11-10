"""Audit hash chain (spec section 10): pure functions, no I/O.

hash = sha256(prev_hash || canonical_json(event)), per organization, seq starting at 1.
`verify_chain` reports the first record whose link is broken, so tampering is located.
"""

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def iso(ts: datetime) -> str:
    """One stable text form for timestamps (UTC, microseconds), whichever driver produced them."""
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    org_id: uuid.UUID
    event_type: str
    actor_id: uuid.UUID | None
    payload: Mapping[str, Any]
    created_at: datetime
    prev_hash: str
    hash: str


def event_document(
    org_id: uuid.UUID,
    seq: int,
    event_type: str,
    actor_id: uuid.UUID | None,
    payload: Mapping[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "org_id": str(org_id),
        "seq": seq,
        "event_type": event_type,
        "actor_id": str(actor_id) if actor_id else None,
        "payload": dict(payload),
        "created_at": iso(created_at),
    }


def chain_hash(prev_hash: str, event: Mapping[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(event)).encode()).hexdigest()


def compute_hash(
    prev_hash: str,
    org_id: uuid.UUID,
    seq: int,
    event_type: str,
    actor_id: uuid.UUID | None,
    payload: Mapping[str, Any],
    created_at: datetime,
) -> str:
    return chain_hash(
        prev_hash, event_document(org_id, seq, event_type, actor_id, payload, created_at)
    )


def verify_chain(records: Iterable[AuditRecord]) -> int | None:
    """Records must be in seq order. Returns the first broken seq, or None if intact."""
    prev = GENESIS_HASH
    expected = 1
    for r in records:
        if r.seq != expected or r.prev_hash != prev:
            return r.seq
        if (
            compute_hash(
                r.prev_hash, r.org_id, r.seq, r.event_type, r.actor_id, r.payload, r.created_at
            )
            != r.hash
        ):
            return r.seq
        prev = r.hash
        expected += 1
    return None
