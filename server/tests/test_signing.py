"""Signed artifacts: a result carries Setu's signature, checkable against the public JWKS."""

import copy
from typing import Any

import pytest

from app.core.signing import SignatureError, Signer, parts_digest, verify_artifact
from tests.a2a_agents import build_v1_agent
from tests.test_facade import RISK, RISK_SKILLS, World, agents
from tests.test_facade_persistence import run_task

SEED = "c2V0dS1kZXYtb25seS1zaWduaW5nLWtleS0zMmJ5dGU="


def test_signature_roundtrip_and_tamper_detection() -> None:
    s = Signer(SEED, "http://hub")
    parts: list[dict[str, Any]] = [{"text": "ok"}, {"data": {"score": 7, "tags": ["a"]}}]
    token = s.sign_artifact(
        task_id="t", room_id="r", artifact_id="a", agent_id="ag", trace_id="tr", parts=parts
    )
    claims = verify_artifact(token, parts, s.jwks())
    assert claims["iss"] == "http://hub" and claims["task"] == "t" and claims["trace"] == "tr"
    # Protobuf turns every number into a float on the wire; the digest must not care.
    wire = copy.deepcopy(parts)
    wire[1]["data"]["score"] = 7.0
    assert parts_digest(wire) == parts_digest(parts)
    verify_artifact(token, wire, s.jwks())
    # Altered parts, a different key, and a mangled token all fail.
    with pytest.raises(SignatureError):
        verify_artifact(token, [{"text": "ok"}, {"data": {"score": 8, "tags": ["a"]}}], s.jwks())
    other = Signer("b3RoZXItc2VlZC1vdGhlci1zZWVkLW90aGVyLXNlZWQ=", "x")
    with pytest.raises(SignatureError):
        verify_artifact(token, parts, other.jwks())
    with pytest.raises(SignatureError):
        verify_artifact(token[:-4] + "AAAA", parts, s.jwks())


def test_results_are_signed_end_to_end(migrated_database_url: str) -> None:
    from tests.test_a2a_flow import hub

    risk = build_v1_agent(RISK, name="Risk", skills=RISK_SKILLS)
    with hub(migrated_database_url, agents(risk)) as (w, _, client):
        world = World(w)
        task = run_task(world, client, f"risk-{w.tag}")
        art = task["artifacts"][0]
        token = art["metadata"]["setu"]["signature"]
        jwks = client.get("/.well-known/jwks.json").json()  # public: no credentials
        claims = verify_artifact(token, art["parts"], jwks)
        assert claims["artifact"] == art["artifactId"] and claims["room"] == world.room1["id"]
        assert claims["sub"] == world.b["participant_id"]

        # The room message that carries the result holds the same signature.
        msgs = w.ok(w.get(f"/v1/rooms/{world.room1['id']}/messages", world.olga), 200)
        results = [m for m in msgs if m["kind"] == "result"]
        assert results and results[0]["body"]["signature"] == token
        with pytest.raises(SignatureError):
            verify_artifact(token, [{"text": "forged"}], jwks)


def test_sdk_verifier_agrees_with_the_server() -> None:
    from setu_sdk import verify as sdk

    s = Signer(SEED, "http://hub")
    parts: list[dict[str, Any]] = [{"text": "héllo"}, {"data": {"n": 3, "x": [1.5, True]}}]
    token = s.sign_artifact(
        task_id="t", room_id="r", artifact_id="a", agent_id="ag", trace_id=None, parts=parts
    )
    assert sdk.parts_digest(parts) == parts_digest(parts)
    assert sdk.verify_artifact(token, parts, s.jwks())["task"] == "t"
    with pytest.raises(sdk.SignatureError):
        sdk.verify_artifact(token, [{"text": "no"}], s.jwks())
