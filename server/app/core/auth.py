"""Credentials: password hashing, JWT access tokens, API keys, and the Principal."""

import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ROLES = ("dept_member", "dept_admin", "org_admin", "auditor")
API_KEY_SCOPES = ("rooms:read", "rooms:write", "messages:send")
API_KEY_PREFIX = "stu"

_hasher = PasswordHasher()
# Verified against when an email is unknown, so login timing does not reveal which emails exist.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


class AuthError(Exception):
    """Credentials missing, malformed, expired or wrong."""


def hash_secret(value: str) -> str:
    return _hasher.hash(value)


def verify_secret(stored_hash: str, value: str) -> bool:
    try:
        return _hasher.verify(stored_hash, value)
    except (VerificationError, InvalidHashError):
        return False


def verify_unknown_user() -> None:
    verify_secret(_DUMMY_HASH, "x")


def create_access_token(
    secret: str, participant_id: uuid.UUID, ttl: timedelta, now: datetime | None = None
) -> str:
    issued = now or datetime.now(UTC)
    claims = {
        "sub": str(participant_id),
        "typ": "access",
        "iat": issued,
        "exp": issued + ttl,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_access_token(secret: str, token: str) -> uuid.UUID:
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
        if claims.get("typ") != "access":
            raise AuthError("wrong token type")
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise AuthError("invalid or expired token") from exc


@dataclass(frozen=True)
class NewApiKey:
    key: str  # shown to the caller exactly once
    prefix: str  # stored in clear; used to look the row up
    key_hash: str  # argon2 hash of the full key


def generate_api_key() -> NewApiKey:
    prefix = secrets.token_hex(4)
    key = f"{API_KEY_PREFIX}_{prefix}_{secrets.token_hex(24)}"
    return NewApiKey(key=key, prefix=prefix, key_hash=hash_secret(key))


def parse_api_key(key: str) -> str | None:
    """Return the lookup prefix if `key` is shaped like one of ours, else None."""
    parts = key.split("_")
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX or len(parts[1]) != 8:
        return None
    return parts[1]


def looks_like_api_key(value: str) -> bool:
    return parse_api_key(value) is not None


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. `scopes is None` means unrestricted (a human session)."""

    participant_id: uuid.UUID
    org_id: uuid.UUID
    department_id: uuid.UUID
    kind: str  # 'human' | 'agent'
    roles: frozenset[str]
    scopes: frozenset[str] | None = None

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    @property
    def is_org_admin(self) -> bool:
        return "org_admin" in self.roles

    def is_dept_admin(self, department_id: uuid.UUID) -> bool:
        return "dept_admin" in self.roles and self.department_id == department_id

    def can_administer(self, department_id: uuid.UUID) -> bool:
        return self.is_org_admin or self.is_dept_admin(department_id)

    def has_scope(self, scope: str) -> bool:
        return self.scopes is None or scope in self.scopes
