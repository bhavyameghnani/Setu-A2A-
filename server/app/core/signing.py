"""Signed artifacts (decision 125): Setu attests to what an agent returned.

When an agent's result is stored, Setu signs a small claim set with its Ed25519 key (a compact
JWS, algorithm EdDSA): which task and room, which agent produced it, which trace, and a SHA-256
digest of the artifact's parts. Anyone holding the artifact can check it against Setu's public
key, published as a JWKS at `/.well-known/jwks.json`, without trusting the channel it arrived on.

This is an attestation by the hub ("this agent returned these parts, through me, in this task"),
not a signature by the origin agent. Pure functions: no I/O, no LLM, no database.
"""

import base64
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ALG = "EdDSA"
TYP = "setu-artifact+jwt"


class SignatureError(Exception):
    pass


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _normalize(value: Any) -> Any:
    """Make equal data serialize equally: protobuf structs turn every number into a float, so
    5.0 and 5 must hash the same."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    return value


def parts_digest(parts: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 (hex) of the parts as canonical JSON: sorted keys, no spaces, UTF-8."""
    canonical = json.dumps(
        _normalize(list(parts)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def key_id(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def jwk_of(public: Ed25519PublicKey) -> dict[str, str]:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64u(raw),
        "kid": key_id(public),
        "alg": ALG,
        "use": "sig",
    }


class Signer:
    def __init__(self, seed_b64: str, issuer: str) -> None:
        try:
            seed = base64.urlsafe_b64decode(seed_b64 + "=" * (-len(seed_b64) % 4))
            self._private = Ed25519PrivateKey.from_private_bytes(seed)
        except ValueError as exc:
            raise ValueError("SIGNING_KEY must be a base64 encoded 32-byte Ed25519 seed") from exc
        self.issuer = issuer
        self.kid = key_id(self._private.public_key())

    def jwks(self) -> dict[str, Any]:
        return {"keys": [jwk_of(self._private.public_key())]}

    def sign_artifact(
        self,
        *,
        task_id: str,
        room_id: str,
        artifact_id: str,
        agent_id: str,
        trace_id: str | None,
        parts: Sequence[Mapping[str, Any]],
        now: float | None = None,
    ) -> str:
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": agent_id,
            "iat": int(now if now is not None else time.time()),
            "task": task_id,
            "room": room_id,
            "artifact": artifact_id,
            "sha256": parts_digest(parts),
        }
        if trace_id:
            claims["trace"] = trace_id
        return jwt.encode(
            claims, self._private, algorithm=ALG, headers={"kid": self.kid, "typ": TYP}
        )


def verify_artifact(
    token: str, parts: Sequence[Mapping[str, Any]], jwks: Mapping[str, Any]
) -> dict[str, Any]:
    """Check `token` against Setu's public keys and the parts actually received. Returns the
    claims; raises `SignatureError` if the signature is bad or the parts were altered."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        match = [k for k in jwks.get("keys", []) if k.get("kid") == kid]
        if not match:
            raise SignatureError(f"no key {kid!r} in the JWKS")
        key = jwt.PyJWK.from_dict(match[0]).key
        claims: dict[str, Any] = jwt.decode(
            token, key, algorithms=[ALG], options={"verify_aud": False}
        )
    except jwt.PyJWTError as exc:
        raise SignatureError(str(exc)) from exc
    if claims.get("sha256") != parts_digest(parts):
        raise SignatureError("the artifact's parts do not match the signed digest")
    return claims


def signer_from_settings(settings: Any) -> Signer:
    """The hub's signer. The issuer is the public URL agents reach Setu at, else just 'setu'."""
    issuer = (settings.public_url or "").rstrip("/") or "setu"
    return Signer(settings.resolved_signing_key(), issuer)
