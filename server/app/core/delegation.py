"""Delegation rules (spec section 10): pure functions, no I/O.

A human grants scopes to an agent. When agent A delegates to B the new grant must be a subset
of what A holds and must not outlive it. Scopes are strings:

    *                everything
    skill:*          any skill
    skill:<id>       one skill (e.g. skill:risk.score)

A scope covers another if it is equal, `*`, or a `prefix:*` wildcard over the same prefix.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

_SCOPE = re.compile(r"^(\*|[a-z][a-z0-9_]*:(\*|[A-Za-z0-9_.\-]+))$")


class ScopeError(ValueError):
    pass


@dataclass(frozen=True)
class Grant:
    id: uuid.UUID
    to_participant_id: uuid.UUID
    scopes: tuple[str, ...]
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


def validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    if not scopes:
        raise ScopeError("at least one scope is required")
    for s in scopes:
        if not _SCOPE.match(s):
            raise ScopeError(f"invalid scope {s!r}")
    return tuple(dict.fromkeys(scopes))


def scope_covers(held: str, needed: str) -> bool:
    if held in ("*", needed):
        return True
    return held.endswith(":*") and needed.startswith(held[:-1])


def covers(held: Sequence[str], needed: str) -> bool:
    return any(scope_covers(h, needed) for h in held)


def is_subset(child: Sequence[str], parent: Sequence[str]) -> bool:
    return all(covers(parent, c) for c in child)


def expiry_ok(child: datetime | None, parent: datetime | None) -> bool:
    """A child grant must expire no later than its parent. No parent expiry = no limit."""
    if parent is None:
        return True
    return child is not None and child <= parent


def required_scopes(skills: Sequence[str]) -> list[str]:
    """What a task needs: one scope per skill, or `skill:*` when it names none."""
    return [f"skill:{s}" for s in skills] or ["skill:*"]


def check_chain(
    chain: Sequence[Grant], requester: uuid.UUID, needed: Sequence[str], now: datetime
) -> str | None:
    """`chain` runs from the grant the requester holds (leaf) up to the root.

    Returns None if the requester may act, else the reason it may not.
    """
    if not chain:
        return "delegation not found"
    if chain[0].to_participant_id != requester:
        return "the delegation was not granted to the requester"
    for g in chain:
        if g.revoked_at is not None:
            return "a delegation in the chain was revoked"
        if g.expires_at is not None and g.expires_at <= now:
            return "a delegation in the chain has expired"
        for scope in needed:
            if not covers(g.scopes, scope):
                return f"scope {scope} is not covered by the delegation"
    return None
