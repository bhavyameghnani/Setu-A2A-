"""Policy engine: pure rules over plain data (spec section 10).

`evaluate(ctx, rules) -> Decision` decides allow / deny / require_approval for one
sender -> recipient hop. No database, no network, no LLM: a test asserts the imports.

Order of evaluation:
  1. cross-org                         -> deny (always)
  2. any matching `deny_talk`          -> deny
  3. any matching `topic_block`        -> deny
  4. not same department and no matching `allow_talk` -> deny (the default)
  5. first matching `require_approval` -> require_approval
  6. otherwise                         -> allow
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

ANY = "*"
RULE_TYPES = ("allow_talk", "deny_talk", "topic_block", "require_approval")
APPROVER_ROLES = ("dept_member", "dept_admin", "org_admin")


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class RuleError(ValueError):
    """A rule document is malformed."""


@dataclass(frozen=True)
class Rule:
    """A parsed rule. Department fields hold a department id (as str) or `*`."""

    type: str
    from_department: str = ANY
    to_department: str = ANY
    topics: tuple[str, ...] = ()  # topic_block: skills/topics that are blocked
    skill: str | None = None  # require_approval: match a skill ...
    kind: str | None = None  # ... or a message kind
    approver_department: str | None = None
    approver_role: str | None = None


@dataclass(frozen=True)
class PolicyRule:
    id: uuid.UUID
    rule: Rule


@dataclass(frozen=True)
class PolicyContext:
    sender_id: uuid.UUID
    recipient_id: uuid.UUID
    sender_org: uuid.UUID
    recipient_org: uuid.UUID
    sender_department: uuid.UUID
    recipient_department: uuid.UUID
    room_id: uuid.UUID
    kind: str
    skills: tuple[str, ...] = ()
    delegation_chain: tuple[uuid.UUID, ...] = ()
    now: datetime | None = None


@dataclass(frozen=True)
class Decision:
    effect: Effect
    reason: str
    rule_id: uuid.UUID | None = None
    approver_department: uuid.UUID | None = None
    approver_role: str | None = None


class PolicyEngine(Protocol):
    def evaluate(self, ctx: PolicyContext) -> Decision: ...


def _str_field(raw: Mapping[str, Any], key: str, required: bool = False) -> str | None:
    value = raw.get(key)
    if value is None:
        if required:
            raise RuleError(f"{key} is required")
        return None
    if not isinstance(value, str) or not value:
        raise RuleError(f"{key} must be a non-empty string")
    return value


def _dept_field(raw: Mapping[str, Any], key: str, required: bool = False) -> str:
    value = _str_field(raw, key, required)
    if value is None:
        return ANY
    if value != ANY:
        try:
            uuid.UUID(value)
        except ValueError:
            raise RuleError(f"{key} must be a department id or '*'") from None
    return value


def parse_rule(raw: Mapping[str, Any]) -> Rule:
    """Validate a rule document (the JSON stored in `policies.rule`)."""
    rtype = _str_field(raw, "type", required=True)
    if rtype not in RULE_TYPES:
        raise RuleError(f"type must be one of {', '.join(RULE_TYPES)}")
    if rtype in ("allow_talk", "deny_talk"):
        return Rule(
            rtype,
            from_department=_dept_field(raw, "from_department", True),
            to_department=_dept_field(raw, "to_department", True),
        )
    if rtype == "topic_block":
        topics = raw.get("topics")
        if (
            not isinstance(topics, list)
            or not topics
            or not all(isinstance(t, str) and t for t in topics)
        ):
            raise RuleError("topics must be a non-empty list of strings")
        return Rule(
            rtype,
            from_department=_dept_field(raw, "from_department"),
            to_department=_dept_field(raw, "to_department"),
            topics=tuple(topics),
        )
    skill = _str_field(raw, "skill")
    kind = _str_field(raw, "kind")
    if (skill is None) == (kind is None):
        raise RuleError("exactly one of skill or kind is required")
    role = _str_field(raw, "approver_role", required=True)
    if role not in APPROVER_ROLES:
        raise RuleError(f"approver_role must be one of {', '.join(APPROVER_ROLES)}")
    approver = _dept_field(raw, "approver_department", True)
    if approver == ANY:
        raise RuleError("approver_department must be a specific department id")
    return Rule(
        rtype,
        from_department=_dept_field(raw, "from_department"),
        to_department=_dept_field(raw, "to_department"),
        skill=skill,
        kind=kind,
        approver_department=approver,
        approver_role=role,
    )


def _dept_match(pattern: str, department: uuid.UUID) -> bool:
    return pattern == ANY or pattern == str(department)


def _hop_match(rule: Rule, ctx: PolicyContext) -> bool:
    return _dept_match(rule.from_department, ctx.sender_department) and _dept_match(
        rule.to_department, ctx.recipient_department
    )


def _approval_match(rule: Rule, ctx: PolicyContext) -> bool:
    if not _hop_match(rule, ctx):
        return False
    if rule.skill is not None:
        return rule.skill == ANY or rule.skill in ctx.skills
    return rule.kind == ctx.kind


class RuleEngine:
    """The in-process engine. OPA/Cedar can replace it behind `PolicyEngine`."""

    def __init__(self, rules: Sequence[PolicyRule]) -> None:
        self._rules = tuple(rules)

    def evaluate(self, ctx: PolicyContext) -> Decision:
        return evaluate(ctx, self._rules)


def evaluate(ctx: PolicyContext, rules: Sequence[PolicyRule]) -> Decision:
    if ctx.sender_org != ctx.recipient_org:
        return Decision(Effect.DENY, "cross-organization traffic is never allowed")
    for pr in rules:
        if pr.rule.type == "deny_talk" and _hop_match(pr.rule, ctx):
            return Decision(Effect.DENY, "a deny_talk rule matches", pr.id)
    for pr in rules:
        if (
            pr.rule.type == "topic_block"
            and _hop_match(pr.rule, ctx)
            and (ANY in pr.rule.topics or any(s in pr.rule.topics for s in ctx.skills))
        ):
            return Decision(Effect.DENY, "a topic_block rule matches", pr.id)
    allow_rule: uuid.UUID | None = None
    if ctx.sender_department != ctx.recipient_department:
        for pr in rules:
            if pr.rule.type == "allow_talk" and _hop_match(pr.rule, ctx):
                allow_rule = pr.id
                break
        else:
            return Decision(
                Effect.DENY, "cross-department traffic is denied until a rule allows it"
            )
    for pr in rules:
        if pr.rule.type == "require_approval" and _approval_match(pr.rule, ctx):
            assert pr.rule.approver_department is not None and pr.rule.approver_role is not None
            return Decision(
                Effect.REQUIRE_APPROVAL,
                "a require_approval rule matches",
                pr.id,
                uuid.UUID(pr.rule.approver_department),
                pr.rule.approver_role,
            )
    return Decision(
        Effect.ALLOW,
        "an allow_talk rule matches" if allow_rule else "same department",
        allow_rule,
    )
