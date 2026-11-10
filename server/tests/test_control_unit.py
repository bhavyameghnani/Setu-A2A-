"""Pure control-plane rules: policy engine, delegation narrowing, audit hash chain."""

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import audit, delegation, lifecycle
from app.core import policy as policy_pkg
from app.core.policy import (
    Effect,
    PolicyContext,
    PolicyRule,
    RuleError,
    evaluate,
    parse_rule,
)
from app.core.policy import engine as policy_engine

ORG, OPS, RISK, COMP = (uuid.uuid4() for _ in range(4))
NOW = datetime(2026, 11, 6, tzinfo=UTC)


def ctx(
    sender_dept: uuid.UUID = OPS,
    recipient_dept: uuid.UUID = RISK,
    kind: str = "task",
    skills: tuple[str, ...] = ("risk.score",),
    recipient_org: uuid.UUID = ORG,
) -> PolicyContext:
    return PolicyContext(
        sender_id=uuid.uuid4(),
        recipient_id=uuid.uuid4(),
        sender_org=ORG,
        recipient_org=recipient_org,
        sender_department=sender_dept,
        recipient_department=recipient_dept,
        room_id=uuid.uuid4(),
        kind=kind,
        skills=skills,
    )


def rule(**raw: object) -> PolicyRule:
    return PolicyRule(uuid.uuid4(), parse_rule(raw))


def allow_ops_risk() -> PolicyRule:
    return rule(type="allow_talk", from_department=str(OPS), to_department=str(RISK))


# ------------------------------------------------------------------ policy


def test_defaults_same_department_allowed_cross_department_denied() -> None:
    assert evaluate(ctx(OPS, OPS), []).effect == Effect.ALLOW
    d = evaluate(ctx(OPS, RISK), [])
    assert d.effect == Effect.DENY and "until a rule allows it" in d.reason


def test_cross_org_is_always_denied_even_with_allow_all() -> None:
    allow_all = rule(type="allow_talk", from_department="*", to_department="*")
    assert evaluate(ctx(recipient_org=uuid.uuid4()), [allow_all]).effect == Effect.DENY


def test_allow_talk_is_directional() -> None:
    allow = allow_ops_risk()
    d = evaluate(ctx(OPS, RISK), [allow])
    assert d.effect == Effect.ALLOW and d.rule_id == allow.id
    assert evaluate(ctx(RISK, OPS), [allow]).effect == Effect.DENY


def test_deny_talk_and_topic_block_beat_allow() -> None:
    allow = rule(type="allow_talk", from_department="*", to_department="*")
    deny = rule(type="deny_talk", from_department=str(OPS), to_department=str(RISK))
    d = evaluate(ctx(OPS, RISK), [allow, deny])
    assert d.effect == Effect.DENY and d.rule_id == deny.id
    assert evaluate(ctx(OPS, COMP), [allow, deny]).effect == Effect.ALLOW
    block = rule(type="topic_block", topics=["risk.score"])
    assert evaluate(ctx(OPS, RISK), [allow, block]).effect == Effect.DENY
    assert evaluate(ctx(OPS, RISK, skills=("other",)), [allow, block]).effect == Effect.ALLOW


def test_require_approval_by_skill_or_kind_only_after_talk_is_allowed() -> None:
    gate = rule(
        type="require_approval",
        skill="risk.score",
        approver_department=str(COMP),
        approver_role="dept_admin",
    )
    assert evaluate(ctx(OPS, RISK), [gate]).effect == Effect.DENY  # not allowed to talk at all
    d = evaluate(ctx(OPS, RISK), [allow_ops_risk(), gate])
    assert d.effect == Effect.REQUIRE_APPROVAL and d.rule_id == gate.id
    assert d.approver_department == COMP and d.approver_role == "dept_admin"
    assert evaluate(ctx(OPS, RISK, skills=("x",)), [allow_ops_risk(), gate]).effect == Effect.ALLOW
    by_kind = rule(
        type="require_approval",
        kind="task",
        approver_department=str(COMP),
        approver_role="org_admin",
    )
    assert evaluate(ctx(OPS, OPS), [by_kind]).effect == Effect.REQUIRE_APPROVAL
    assert evaluate(ctx(OPS, OPS, kind="text"), [by_kind]).effect == Effect.ALLOW


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"type": "nope"},
        {"type": "allow_talk", "from_department": str(OPS)},
        {"type": "allow_talk", "from_department": "ops", "to_department": "*"},
        {"type": "topic_block", "topics": []},
        {"type": "require_approval", "approver_department": str(COMP), "approver_role": "x"},
        {
            "type": "require_approval",
            "skill": "a",
            "kind": "task",
            "approver_department": str(COMP),
            "approver_role": "dept_admin",
        },
        {
            "type": "require_approval",
            "skill": "a",
            "approver_department": "*",
            "approver_role": "dept_admin",
        },
    ],
)
def test_malformed_rules_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(RuleError):
        parse_rule(raw)


# ------------------------------------------------------------------ delegation


def test_scope_cover_and_subset() -> None:
    assert delegation.scope_covers("*", "skill:x")
    assert delegation.scope_covers("skill:*", "skill:x")
    assert delegation.scope_covers("skill:x", "skill:x")
    assert not delegation.scope_covers("skill:x", "skill:y")
    assert not delegation.scope_covers("skill:x", "skill:*")
    assert not delegation.scope_covers("skill:*", "other:x")
    assert delegation.is_subset(["skill:a", "skill:b"], ["skill:*"])
    assert not delegation.is_subset(["skill:a", "other:b"], ["skill:*"])
    assert not delegation.is_subset(["*"], ["skill:*"])


def test_scope_validation() -> None:
    assert delegation.validate_scopes(["skill:a", "skill:a", "*"]) == ("skill:a", "*")
    for bad in ([], [""], ["skill"], ["Skill:a"], ["skill: a"]):
        with pytest.raises(delegation.ScopeError):
            delegation.validate_scopes(bad)


def test_child_may_not_outlive_parent() -> None:
    soon, later = NOW + timedelta(hours=1), NOW + timedelta(hours=2)
    assert delegation.expiry_ok(soon, later)
    assert not delegation.expiry_ok(later, soon)
    assert not delegation.expiry_ok(None, soon)  # no expiry would outlive a parent that has one
    assert delegation.expiry_ok(None, None) and delegation.expiry_ok(soon, None)


def test_check_chain() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    root = delegation.Grant(uuid.uuid4(), a, ("skill:*",))
    leaf = delegation.Grant(uuid.uuid4(), b, ("skill:risk.score",))
    need = delegation.required_scopes(["risk.score"])
    assert delegation.check_chain([leaf, root], b, need, NOW) is None
    assert "not granted" in str(delegation.check_chain([leaf, root], a, need, NOW))
    assert "not covered" in str(
        delegation.check_chain([leaf, root], b, delegation.required_scopes(["x"]), NOW)
    )
    assert delegation.required_scopes([]) == ["skill:*"]
    dead = delegation.Grant(leaf.id, b, leaf.scopes, revoked_at=NOW)
    assert "revoked" in str(delegation.check_chain([dead, root], b, need, NOW))
    old = delegation.Grant(root.id, a, root.scopes, expires_at=NOW - timedelta(seconds=1))
    assert "expired" in str(delegation.check_chain([leaf, old], b, need, NOW))
    assert delegation.check_chain([], b, need, NOW) == "delegation not found"


# ------------------------------------------------------------------ audit chain


def records(n: int) -> list[audit.AuditRecord]:
    org, prev, out = uuid.uuid4(), audit.GENESIS_HASH, []
    for seq in range(1, n + 1):
        at = NOW + timedelta(seconds=seq)
        payload = {"n": seq, "nested": {"b": 1, "a": [1, 2]}}
        h = audit.compute_hash(prev, org, seq, "e.test", None, payload, at)
        out.append(audit.AuditRecord(seq, org, "e.test", None, payload, at, prev, h))
        prev = h
    return out


def test_intact_chain_verifies_and_key_order_does_not_matter() -> None:
    recs = records(5)
    assert audit.verify_chain(recs) is None
    assert audit.verify_chain([]) is None
    reordered = {"nested": {"a": [1, 2], "b": 1}, "n": 3}  # as JSONB may hand it back
    r = recs[2]
    same = audit.AuditRecord(
        r.seq, r.org_id, r.event_type, r.actor_id, reordered, r.created_at, r.prev_hash, r.hash
    )
    assert audit.verify_chain([*recs[:2], same, *recs[3:]]) is None


def test_tampering_is_located() -> None:
    recs = records(5)
    r = recs[2]
    edited = audit.AuditRecord(
        r.seq, r.org_id, r.event_type, r.actor_id, {"n": 99}, r.created_at, r.prev_hash, r.hash
    )
    assert audit.verify_chain([*recs[:2], edited, *recs[3:]]) == 3
    assert audit.verify_chain([*recs[:1], *recs[2:]]) == 3  # a deleted record breaks seq and link
    assert audit.verify_chain(recs[1:]) == 2  # the head was cut off


# ------------------------------------------------------------------ lifecycle & purity


def test_release_gated_only_from_input_required() -> None:
    assert (
        lifecycle.release_gated(lifecycle.TaskState.INPUT_REQUIRED) == lifecycle.TaskState.SUBMITTED
    )
    for s in lifecycle.TaskState:
        if s != lifecycle.TaskState.INPUT_REQUIRED:
            with pytest.raises(lifecycle.InvalidTransitionError):
                lifecycle.release_gated(s)
    # the general machine still never goes back to submitted
    with pytest.raises(lifecycle.InvalidTransitionError):
        lifecycle.next_task_state(lifecycle.TaskState.INPUT_REQUIRED, lifecycle.TaskState.SUBMITTED)


FORBIDDEN = (
    "openai", "anthropic", "langchain", "litellm", "llm", "httpx", "requests", "aiohttp",
    "sqlalchemy", "asyncpg", "fastapi", "socket",
)  # fmt: skip


@pytest.mark.parametrize("mod", [policy_engine, policy_pkg, delegation, audit])
def test_control_core_is_pure(mod: object) -> None:
    src = Path(str(mod.__file__)).read_text()  # type: ignore[attr-defined]
    imported: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    bad = [m for m in imported if any(f in m.lower() for f in FORBIDDEN)]
    assert not bad, f"{mod.__file__} must stay pure, found: {bad}"  # type: ignore[attr-defined]
