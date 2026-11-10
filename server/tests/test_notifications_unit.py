"""Notification templates: deterministic sentences, no I/O."""

import ast
from pathlib import Path

import pytest

from app.core import notifications as n
from app.core.notifications import Event, Facts

F = Facts(
    requester="Ops-Agent",
    assignee="Risk-Agent",
    actor="Carl",
    what="risk.score",
    detail="boom",
    has_result=True,
    department="Compliance",
)


def test_the_headline_sentence() -> None:
    assert (
        n.summary(Event.TASK_COMPLETED, F)
        == "Risk-Agent finished risk.score for Ops-Agent. Result attached."
    )
    no_result = Facts(requester="Ops-Agent", assignee="Risk-Agent", what="risk.score")
    assert (
        n.summary(Event.TASK_COMPLETED, no_result)
        == "Risk-Agent finished risk.score for Ops-Agent."
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (Event.TASK_STARTED, "Risk-Agent started risk.score for Ops-Agent."),
        (Event.TASK_FAILED, "Risk-Agent failed risk.score for Ops-Agent: boom."),
        (Event.TASK_CANCELED, "risk.score for Ops-Agent was canceled: boom."),
        (
            Event.TASK_REJECTED,
            "risk.score requested by Ops-Agent from Risk-Agent was rejected: boom.",
        ),
        (Event.TASK_INPUT_REQUIRED, "Risk-Agent needs input on risk.score for Ops-Agent: boom."),
        (Event.APPROVAL_REQUESTED, "Approval needed from Compliance: boom"),
        (Event.APPROVAL_GRANTED, "Carl approved: boom"),
        (Event.APPROVAL_DENIED, "Carl denied: boom"),
        (Event.POLICY_DENIED, "Ops-Agent was not allowed to ask Risk-Agent for risk.score: boom."),
        (Event.AGENT_REGISTERED, "Risk-Agent was registered for Compliance."),
        (Event.AGENT_CARD_CHANGED, "The Agent Card of Risk-Agent changed: boom."),
    ],
)
def test_every_event_has_a_template(event: Event, expected: str) -> None:
    assert n.summary(event, F) == expected


def test_every_event_is_covered() -> None:
    for e in Event:
        assert n.summary(e, F)


def test_empty_detail_leaves_no_dangling_colon() -> None:
    bare = Facts(requester="A", assignee="B", what="x")
    assert n.summary(Event.TASK_FAILED, bare) == "B failed x for A."
    assert n.summary(Event.TASK_CANCELED, bare) == "x for A was canceled."


def test_long_or_multiline_detail_is_clipped_and_flattened() -> None:
    out = n.summary(
        Event.TASK_FAILED,
        Facts(requester="A", assignee="B", what="x", detail="l1\n\n  l2 " + "z" * 500),
    )
    assert "\n" not in out and "  " not in out and out.endswith("….")
    assert len(out) < n.MAX_DETAIL + 60


def test_state_to_event() -> None:
    assert n.event_for_state("completed") == Event.TASK_COMPLETED
    assert n.event_for_state("input_required") == Event.TASK_INPUT_REQUIRED
    assert n.event_for_state("working") is None and n.event_for_state("submitted") is None


def test_templates_are_pure() -> None:
    tree = ast.parse(Path(n.__file__).read_text())
    imported = [
        *(x.module or "" for x in ast.walk(tree) if isinstance(x, ast.ImportFrom)),
        *(a.name for x in ast.walk(tree) if isinstance(x, ast.Import) for a in x.names),
    ]
    bad = [
        m
        for m in imported
        if any(
            f in m.lower() for f in ("llm", "openai", "anthropic", "httpx", "sqlalchemy", "fastapi")
        )
    ]
    assert not bad
