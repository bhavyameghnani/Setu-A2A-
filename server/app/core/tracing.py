"""W3C Trace Context helpers: pure functions, no I/O (spec section 11).

A trace id is 32 lowercase hex characters and a span id is 16; neither may be all zeros.
`traceparent` = `00-<trace_id>-<span_id>-<flags>`.
"""

import re
import secrets
from dataclasses import dataclass

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

SPAN_KINDS = ("message", "a2a_call", "policy", "delegation", "approval")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    sampled: bool = True


def new_trace_id() -> str:
    while True:
        t = secrets.token_hex(16)
        if t != _ZERO_TRACE:
            return t


def new_span_id() -> str:
    while True:
        s = secrets.token_hex(8)
        if s != _ZERO_SPAN:
            return s


def format_traceparent(trace_id: str, span_id: str | None = None, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id or new_span_id()}-{'01' if sampled else '00'}"


def parse_traceparent(value: str | None) -> TraceContext | None:
    """Returns None for anything malformed, so a bad header starts a fresh trace instead of
    failing the call. Version `ff` is invalid; unknown future versions are read as 00."""
    if not value:
        return None
    m = _TRACEPARENT.match(value.strip().lower())
    if m is None:
        return None
    version, trace_id, span_id, flags = m.groups()
    if version == "ff" or trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return TraceContext(trace_id, span_id, bool(int(flags, 16) & 1))
