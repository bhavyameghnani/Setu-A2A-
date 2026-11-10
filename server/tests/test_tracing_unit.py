"""W3C trace context helpers and the facade's address scrubbing (pure functions)."""

import ast
from pathlib import Path

import pytest

from app.a2a.mapping import scrub
from app.core import tracing

TID, SID = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"


def test_format_and_parse_round_trip() -> None:
    tp = tracing.format_traceparent(TID, SID)
    assert tp == f"00-{TID}-{SID}-01"
    ctx = tracing.parse_traceparent(tp)
    assert ctx == tracing.TraceContext(TID, SID, True)
    assert tracing.parse_traceparent(tracing.format_traceparent(TID, SID, sampled=False)) == (
        tracing.TraceContext(TID, SID, False)
    )
    assert tracing.parse_traceparent(f"  {tp.upper()}  ") == ctx  # case and spaces are tolerated


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "junk",
        f"00-{TID}-{SID}",  # no flags
        f"00-{TID[:-1]}-{SID}-01",  # short trace id
        f"00-{TID}-{SID[:-1]}-01",
        f"00-{'0' * 32}-{SID}-01",  # all-zero trace id
        f"00-{TID}-{'0' * 16}-01",  # all-zero span id
        f"ff-{TID}-{SID}-01",  # version ff is invalid
        f"00-{TID}-{SID}-01-extra",
        f"00-{'g' * 32}-{SID}-01",
    ],
)
def test_malformed_traceparent_is_ignored(bad: str | None) -> None:
    assert tracing.parse_traceparent(bad) is None


def test_generated_ids_are_valid() -> None:
    for _ in range(50):
        t, s = tracing.new_trace_id(), tracing.new_span_id()
        assert len(t) == 32 and len(s) == 16 and int(t, 16) and int(s, 16)
        assert tracing.parse_traceparent(tracing.format_traceparent(t, s)) is not None
    assert tracing.parse_traceparent(tracing.format_traceparent(TID)) is not None  # span generated


def test_tracing_module_is_pure() -> None:
    forbidden = ("httpx", "sqlalchemy", "fastapi", "asyncpg", "openai", "anthropic", "requests")
    imported = [
        n.module or ""
        for n in ast.walk(ast.parse(Path(tracing.__file__).read_text()))
        if isinstance(n, ast.ImportFrom)
    ] + [
        a.name
        for n in ast.walk(ast.parse(Path(tracing.__file__).read_text()))
        if isinstance(n, ast.Import)
        for a in n.names
    ]
    assert not [m for m in imported if any(f in m for f in forbidden)]


def test_scrub_hides_addresses() -> None:
    out = scrub(
        "blocked unsafe agent address: http://10.0.0.5:8080/rpc, also https://x.example/a?b=1"
    )
    assert "10.0.0.5" not in out and "x.example" not in out and out.count("[address hidden]") == 2
    assert scrub("agent timed out") == "agent timed out"
