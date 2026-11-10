"""Register an existing A2A agent by Agent Card URL, and keep its card fresh (spec 8.2)."""

import asyncio
import contextlib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a import versions
from app.a2a.client import A2AClient, A2AError, AgentEndpoint, auth_headers
from app.a2a.ssrf import UnsafeUrlError, validate_agent_url
from app.a2a.types import A2AVersion, CardInfo
from app.core import secrets
from app.core.notifications import Event
from app.db.models import Agent, Department, Participant
from app.db.notifications import notify_agent

log = structlog.get_logger()


class RegistrationError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Registered:
    participant: Participant
    agent: Agent
    warnings: list[str]


def card_hash(card: dict[str, Any]) -> str:
    canonical = json.dumps(card, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")
    return slug or "agent"


def skills_json(info: CardInfo) -> list[dict[str, Any]]:
    return [
        {"id": s.id, "name": s.name, "description": s.description, "tags": list(s.tags)}
        for s in info.skills
    ]


def stored_credentials(key: str, agent: Agent) -> dict[str, Any] | None:
    return secrets.decrypt_json(key, agent.auth_config) if agent.auth_config else None


def endpoint_for(agent: Agent, key: str) -> AgentEndpoint:
    if not agent.a2a_rpc_url or not agent.a2a_version:
        raise A2AError("agent has no stored A2A endpoint")
    return AgentEndpoint(
        rpc_url=agent.a2a_rpc_url,
        version=A2AVersion(agent.a2a_version),
        streaming=agent.streaming,
        timeout=float(agent.timeout_seconds),
        headers=auth_headers(stored_credentials(key, agent)),
    )


def _candidate_urls(card_url: str) -> list[str]:
    parts = urlsplit(card_url)
    if parts.path in ("", "/") or parts.path in versions.CARD_PATHS:
        base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        first = [card_url] if parts.path in versions.CARD_PATHS else []
        return first + [base + p for p in versions.CARD_PATHS if base + p not in first]
    return [card_url]


async def fetch_card(
    client: A2AClient, card_url: str, headers: dict[str, str], *, allow_private: bool
) -> tuple[dict[str, Any], CardInfo]:
    """Fetch and validate a card. Raises RegistrationError with a user-presentable message."""
    last: str = "agent card not found"
    for url in _candidate_urls(card_url):
        try:
            await validate_agent_url(url, allow_private=allow_private)
        except UnsafeUrlError as exc:
            raise RegistrationError(str(exc)) from exc
        try:
            card = await client.fetch_card(url, headers)
        except A2AError as exc:
            last = str(exc)
            continue
        try:
            info = versions.parse_card(card)
        except versions.CardError as exc:
            raise RegistrationError(f"unusable agent card: {exc}") from exc
        try:
            await validate_agent_url(info.rpc_url, allow_private=allow_private)
        except UnsafeUrlError as exc:
            raise RegistrationError(f"card points at an unsafe endpoint: {exc}") from exc
        return card, info
    raise RegistrationError(last, 502)


def _apply_card(agent: Agent, card: dict[str, Any], info: CardInfo) -> None:
    agent.a2a_card_json = card
    agent.a2a_protocol_versions = list(info.protocol_versions)
    agent.a2a_version = info.version.value
    agent.a2a_rpc_url = info.rpc_url
    agent.streaming = info.streaming
    agent.skills = skills_json(info)
    agent.card_hash = card_hash(card)
    agent.card_fetched_at = datetime.now(UTC)


async def register_agent(
    session: AsyncSession,
    client: A2AClient,
    *,
    org_id: uuid.UUID,
    department: Department,
    card_url: str,
    auth: dict[str, Any] | None,
    handle: str | None,
    display_name: str | None,
    timeout_seconds: int,
    max_retries: int,
    allow_private: bool,
    encryption_key: str,
) -> Registered:
    existing = await session.execute(
        select(Agent.participant_id)
        .join(Participant, Participant.id == Agent.participant_id)
        .where(Participant.org_id == org_id, Agent.a2a_card_url == card_url)
    )
    if existing.first() is not None:
        raise RegistrationError("this agent card is already registered in your org", 409)

    card, info = await fetch_card(client, card_url, auth_headers(auth), allow_private=allow_private)
    warnings: list[str] = []
    if info.security and not auth:
        names = ", ".join(s.name for s in info.security)
        warnings.append(f"card declares security schemes ({names}) but no credentials were given")

    participant = Participant(
        org_id=org_id,
        department_id=department.id,
        kind="agent",
        display_name=display_name or info.name,
        handle=handle or slugify(info.name),
    )
    session.add(participant)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise RegistrationError(
            f"handle {handle or slugify(info.name)!r} is already taken in this org; pass `handle`",
            409,
        ) from None
    agent = Agent(
        participant_id=participant.id,
        owner_department_id=department.id,
        transport="a2a_remote",
        a2a_card_url=card_url,
        auth_config=secrets.encrypt_json(encryption_key, auth) if auth else None,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        enabled=True,
    )
    _apply_card(agent, card, info)
    session.add(agent)
    await notify_agent(session, participant, Event.AGENT_REGISTERED)
    await session.commit()
    await session.refresh(participant)
    await session.refresh(agent)
    return Registered(participant, agent, warnings)


async def refresh_agent(
    session: AsyncSession,
    client: A2AClient,
    agent: Agent,
    *,
    allow_private: bool,
    encryption_key: str,
) -> bool:
    """Re-fetch the card. Returns True if it changed. Raises RegistrationError on failure."""
    if agent.transport != "a2a_remote" or not agent.a2a_card_url:
        raise RegistrationError("only a2a_remote agents have a card to refresh", 409)
    creds = stored_credentials(encryption_key, agent)
    card, info = await fetch_card(
        client, agent.a2a_card_url, auth_headers(creds), allow_private=allow_private
    )
    changed = card_hash(card) != agent.card_hash
    if changed:
        old_skills = {str(x.get("id")) for x in agent.skills or [] if isinstance(x, dict)}
        _apply_card(agent, card, info)
        new_skills = {s.id for s in info.skills}
        added, removed = sorted(new_skills - old_skills), sorted(old_skills - new_skills)
        note = "; ".join(
            x
            for x in (
                f"new skills: {', '.join(added)}" if added else "",
                f"removed skills: {', '.join(removed)}" if removed else "",
            )
            if x
        )
        owner = await session.get(Participant, agent.participant_id)
        if owner is not None:
            await notify_agent(session, owner, Event.AGENT_CARD_CHANGED, note)
    else:
        agent.card_fetched_at = datetime.now(UTC)
    await session.commit()
    return changed


async def refresh_all(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: A2AClient,
    *,
    allow_private: bool,
    encryption_key: str,
) -> int:
    """Refresh every enabled remote agent. One bad agent never stops the others."""
    changed = 0
    async with sessionmaker() as session:
        ids = list(
            (
                await session.execute(
                    select(Agent.participant_id).where(
                        Agent.transport == "a2a_remote", Agent.enabled.is_(True)
                    )
                )
            ).scalars()
        )
    for pid in ids:
        async with sessionmaker() as session:
            agent = await session.get(Agent, pid)
            if agent is None:
                continue
            try:
                if await refresh_agent(
                    session,
                    client,
                    agent,
                    allow_private=allow_private,
                    encryption_key=encryption_key,
                ):
                    changed += 1
                    log.info("agent_card_changed", agent=str(pid))
            except (RegistrationError, secrets.DecryptionError) as exc:
                log.warning("agent_card_refresh_failed", agent=str(pid), error=str(exc))
    return changed


async def refresh_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: A2AClient,
    interval: float,
    *,
    allow_private: bool,
    encryption_key: str,
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_all(
                sessionmaker, client, allow_private=allow_private, encryption_key=encryption_key
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("card_refresh_run_failed", error=str(exc))


async def stop_task(task: "asyncio.Task[None] | None") -> None:
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
