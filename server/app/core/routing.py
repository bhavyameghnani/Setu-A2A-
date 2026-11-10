"""Deterministic routing: who is *addressed* by a message.

Pure functions over plain data. No database, no network, and no LLM, ever (spec section 9).
A test asserts this module imports nothing LLM-shaped.

A participant is addressed if it is in `mentions`, is the assignee of the referenced task, or
holds a matching subscription. Only addressed participants are expected to act; everyone else
who can see the room just receives the message as context.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Informational messages never ask anyone to act, even if they mention someone.
INFORMATIONAL_KINDS = frozenset({"activity", "system"})
ANY = ""  # wildcard in a subscription's kind or skill


@dataclass(frozen=True)
class Member:
    participant_id: uuid.UUID
    role: str  # 'owner' | 'member' | 'observer'


@dataclass(frozen=True)
class Subscription:
    participant_id: uuid.UUID
    kind: str = ANY
    skill: str = ANY


@dataclass(frozen=True)
class RoutingInput:
    sender_id: uuid.UUID
    kind: str
    mentions: Sequence[uuid.UUID] = ()
    skills: Sequence[str] = ()
    assignee_id: uuid.UUID | None = None  # assignee of the referenced task, once tasks exist


def subscription_matches(sub: Subscription, msg: RoutingInput) -> bool:
    kind_ok = sub.kind == ANY or sub.kind == msg.kind
    skill_ok = sub.skill == ANY or sub.skill in msg.skills
    return kind_ok and skill_ok


def addressed_participants(
    msg: RoutingInput,
    members: Iterable[Member],
    subscriptions: Iterable[Subscription] = (),
) -> tuple[uuid.UUID, ...]:
    """Addressed participants in a stable order (sorted by id).

    Candidates are room members who are not observers and not the sender. Mentions or a task
    assignment of anyone else (a non-member, an observer) are ignored, not errors.
    """
    if msg.kind in INFORMATIONAL_KINDS:
        return ()
    eligible = {
        m.participant_id
        for m in members
        if m.role != "observer" and m.participant_id != msg.sender_id
    }
    chosen: set[uuid.UUID] = set(msg.mentions) & eligible
    if msg.assignee_id is not None and msg.assignee_id in eligible:
        chosen.add(msg.assignee_id)
    for sub in subscriptions:
        if sub.participant_id in eligible and subscription_matches(sub, msg):
            chosen.add(sub.participant_id)
    return tuple(sorted(chosen, key=str))


def is_addressed(participant_id: uuid.UUID, addressed: Iterable[uuid.UUID]) -> bool:
    """The only-act-when-addressed guard, shared by the server and (later) the SDK."""
    return participant_id in set(addressed)
