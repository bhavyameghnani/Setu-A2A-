"""Small helpers shared by the message path and the control plane."""

from typing import Any


def message_skills(body: dict[str, Any]) -> list[str]:
    """Skills a message is about: body.skill (str) and/or body.skills (list of str)."""
    out: list[str] = []
    one = body.get("skill")
    if isinstance(one, str):
        out.append(one)
    many = body.get("skills")
    if isinstance(many, list):
        out.extend(x for x in many if isinstance(x, str))
    return out
