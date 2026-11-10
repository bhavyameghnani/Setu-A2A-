"""Check a signed artifact against Setu's public key. Requires `pip install setu-sdk[verify]`.

    jwks = fetch_jwks("http://localhost:8000")
    claims = verify_artifact(artifact["metadata"]["setu"]["signature"], artifact["parts"], jwks)

Setu attests that the agent named in the claims returned exactly these parts in that task and
room. The digest is SHA-256 over the parts as canonical JSON (sorted keys, no spaces, UTF-8,
whole-number floats written as integers), matching `app.core.signing` on the server.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx
import jwt


class SignatureError(Exception):
    pass


def fetch_jwks(base_url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    r = httpx.get(base_url.rstrip("/") + "/.well-known/jwks.json", timeout=timeout)
    r.raise_for_status()
    return cast(dict[str, Any], r.json())


def _normalize(value: Any) -> Any:
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
    canonical = json.dumps(
        _normalize(list(parts)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_artifact(
    token: str, parts: Sequence[Mapping[str, Any]], jwks: Mapping[str, Any]
) -> dict[str, Any]:
    """Returns the signed claims, or raises `SignatureError` if the signature is bad or the
    parts differ from what was signed."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        match = [k for k in jwks.get("keys", []) if k.get("kid") == kid]
        if not match:
            raise SignatureError(f"no key {kid!r} in the JWKS")
        key = jwt.PyJWK.from_dict(match[0]).key
        claims: dict[str, Any] = jwt.decode(token, key, algorithms=["EdDSA"])
    except jwt.PyJWTError as exc:
        raise SignatureError(str(exc)) from exc
    if claims.get("sha256") != parts_digest(parts):
        raise SignatureError("the artifact's parts do not match the signed digest")
    return claims
