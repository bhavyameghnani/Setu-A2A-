"""Encryption at rest for agent credentials (Fernet: AES-128-CBC + HMAC-SHA256).

The key comes from the environment (ENCRYPTION_KEY). Rotating it needs a re-encrypt migration;
supporting multiple keys (MultiFernet) is a later step.
"""

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class DecryptionError(Exception):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_json(key: str, value: dict[str, Any]) -> dict[str, Any]:
    """Returns a small JSONB-friendly wrapper, so the column can say what it holds."""
    token = Fernet(key.encode()).encrypt(json.dumps(value, separators=(",", ":")).encode())
    return {"v": 1, "ct": token.decode()}


def decrypt_json(key: str, wrapped: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = Fernet(key.encode()).decrypt(str(wrapped["ct"]).encode())
        out = json.loads(raw)
    except (InvalidToken, KeyError, ValueError) as exc:
        raise DecryptionError("cannot decrypt stored credentials (wrong ENCRYPTION_KEY?)") from exc
    if not isinstance(out, dict):
        raise DecryptionError("stored credentials are malformed")
    return out
