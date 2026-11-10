import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core import auth

SECRET = "x" * 40


def test_password_hash_roundtrip() -> None:
    h = auth.hash_secret("hunter2hunter2")
    assert h != "hunter2hunter2"
    assert auth.verify_secret(h, "hunter2hunter2")
    assert not auth.verify_secret(h, "wrong")
    assert not auth.verify_secret("not-a-hash", "anything")


def test_jwt_roundtrip_and_rejections() -> None:
    pid = uuid.uuid4()
    token = auth.create_access_token(SECRET, pid, timedelta(minutes=5))
    assert auth.decode_access_token(SECRET, token) == pid
    with pytest.raises(auth.AuthError):
        auth.decode_access_token("y" * 40, token)  # wrong key
    expired = auth.create_access_token(
        SECRET, pid, timedelta(minutes=1), now=datetime.now(UTC) - timedelta(hours=1)
    )
    with pytest.raises(auth.AuthError):
        auth.decode_access_token(SECRET, expired)
    with pytest.raises(auth.AuthError):
        auth.decode_access_token(SECRET, "garbage")


def test_api_key_shape_and_hash() -> None:
    new = auth.generate_api_key()
    assert auth.parse_api_key(new.key) == new.prefix
    assert auth.verify_secret(new.key_hash, new.key)
    assert new.key not in new.key_hash
    assert auth.parse_api_key("stu_short_x") is None
    assert auth.parse_api_key("eyJhbGciOi.jwt.like") is None
