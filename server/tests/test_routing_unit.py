"""Pure unit tests for routing / lifecycle / replay, plus the "no LLM in routing" guard."""

import ast
import uuid
from pathlib import Path

import pytest

from app.core import routing
from app.core.lifecycle import (
    DeliveryStatus,
    InvalidTransitionError,
    next_delivery_status,
)
from app.core.replay import SnapshotItem, advance_cursor, resume_seq, summarize_segment
from app.core.routing import Member, RoutingInput, Subscription, addressed_participants

A, B, C, D, S = (uuid.uuid4() for _ in range(5))
MEMBERS = [Member(A, "member"), Member(B, "member"), Member(C, "observer"), Member(S, "owner")]


def route(**kw: object) -> set[uuid.UUID]:
    base: dict[str, object] = {"sender_id": S, "kind": "text"}
    subs = kw.pop("subs", ())
    return set(addressed_participants(RoutingInput(**{**base, **kw}), MEMBERS, subs))  # type: ignore[arg-type]


def test_mentions_address_only_mentioned_members() -> None:
    assert route(mentions=[A]) == {A}
    assert route(mentions=[A, B]) == {A, B}
    assert route() == set()  # nobody mentioned, nobody subscribed: context only


def test_sender_observers_and_non_members_are_never_addressed() -> None:
    assert route(mentions=[S]) == set()
    assert route(mentions=[C]) == set()  # observer
    assert route(mentions=[D]) == set()  # not in the room


def test_assignee_is_addressed() -> None:
    assert route(assignee_id=B) == {B}
    assert route(assignee_id=D) == set()


def test_subscriptions_match_on_kind_and_skill() -> None:
    subs = [Subscription(A, kind="task", skill="risk.score"), Subscription(B, kind="task")]
    assert route(kind="task", skills=["risk.score"], subs=subs) == {A, B}
    assert route(kind="task", skills=["other"], subs=subs) == {B}
    assert route(kind="text", skills=["risk.score"], subs=subs) == set()
    assert route(kind="task", subs=[Subscription(A)]) == {A}  # wildcard subscription


def test_informational_kinds_address_nobody() -> None:
    assert route(kind="activity", mentions=[A]) == set()
    assert route(kind="system", subs=[Subscription(A)]) == set()


def test_result_is_stable_and_deduplicated() -> None:
    msg = RoutingInput(sender_id=S, kind="text", mentions=[B, A, A])
    subs = [Subscription(A), Subscription(B)]
    first = addressed_participants(msg, MEMBERS, subs)
    assert first == addressed_participants(msg, list(reversed(MEMBERS)), subs)
    assert len(first) == 2 == len(set(first))
    assert routing.is_addressed(A, first) and not routing.is_addressed(S, first)


def test_delivery_status_machine() -> None:
    P, K, OK, X = (
        DeliveryStatus.PENDING,
        DeliveryStatus.ACKED,
        DeliveryStatus.COMPLETED,
        DeliveryStatus.FAILED,
    )
    assert next_delivery_status(P, K) == K
    assert next_delivery_status(K, OK) == OK
    assert next_delivery_status(P, X) == X
    assert next_delivery_status(OK, OK) == OK  # retry is a no-op
    for cur, tgt in [(OK, X), (X, OK), (OK, K), (K, P), (K, DeliveryStatus.PENDING)]:
        with pytest.raises(InvalidTransitionError):
            next_delivery_status(cur, tgt)


def test_cursor_rules() -> None:
    assert resume_seq(None, None) == 0
    assert resume_seq(None, 7) == 7
    assert resume_seq(3, 7) == 3  # explicit wins
    assert resume_seq(-5, 7) == 0
    assert advance_cursor(5, 3, 10) == 5  # never rewinds
    assert advance_cursor(5, 9, 10) == 9
    with pytest.raises(ValueError):
        advance_cursor(5, 11, 10)
    with pytest.raises(ValueError):
        advance_cursor(5, -1, 10)


def test_summary_is_deterministic() -> None:
    items = [
        SnapshotItem(1, "text", "ana"),
        SnapshotItem(2, "task", "bot"),
        SnapshotItem(3, "text", "ana"),
    ]
    out = summarize_segment(items)
    assert out == "Messages 1-3 (3): 1 task, 2 text. From: ana (2), bot (1)."
    assert summarize_segment(items) == out
    assert summarize_segment([]) == "No messages."


FORBIDDEN = (
    "anthropic",
    "openai",
    "langchain",
    "langgraph",
    "litellm",
    "cohere",
    "mistral",
    "google.generativeai",
    "ollama",
    "transformers",
    "llm",
    "httpx",
    "requests",
    "aiohttp",
)


def test_routing_module_has_no_llm_or_network_imports() -> None:
    src = Path(routing.__file__).read_text()
    imported: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    bad = [m for m in imported if any(f in m.lower() for f in FORBIDDEN)]
    assert not bad, f"core/routing.py must stay deterministic and offline, found: {bad}"
