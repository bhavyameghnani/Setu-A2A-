"""Compliance-Agent: served with `setu_sdk.a2a_serve` from a LangGraph graph.

This is the "I have a LangGraph agent, make it A2A" path: the graph below knows nothing about
Setu. `graph_handler` + `build_a2a_app` turn it into an A2A server with an Agent Card, and
Compliance registers it in Setu by pasting that card's URL.
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from setu_sdk.a2a_serve import Skill, build_a2a_app, graph_handler

REVIEW_THRESHOLD = 10_000  # claims above this need a human review (a stand-in for a real policy)

SKILLS = [
    Skill(
        "compliance.check", "Policy check", "Checks a claim against claims policy", ["compliance"]
    )
]


class State(TypedDict, total=False):
    text: str
    data: list[Any]
    metadata: dict[str, Any]
    claim: int | None
    amount: float
    output: dict[str, Any]


def build_graph() -> Any:
    def read(state: State) -> State:
        data = next((d for d in state.get("data", []) if isinstance(d, dict)), {})
        return {"claim": data.get("claim"), "amount": float(data.get("amount", 0))}

    def check(state: State) -> State:
        amount = state["amount"]
        if amount > REVIEW_THRESHOLD:
            verdict, reason = (
                "review_required",
                f"amount {amount:,.0f} is over {REVIEW_THRESHOLD:,}",
            )
        else:
            verdict, reason = "ok", "within policy"
        return {"output": {"claim": state.get("claim"), "verdict": verdict, "reason": reason}}

    g = StateGraph(State)
    g.add_node("read", read)
    g.add_node("check", check)
    g.add_edge(START, "read")
    g.add_edge("read", "check")
    g.add_edge("check", END)
    return g.compile()


def build_app(public_url: str, *, bearer_token: str | None = None) -> Any:
    return build_a2a_app(
        graph_handler(build_graph()),
        name="Compliance-Agent",
        description="Checks claims against claims policy (LangGraph, served over A2A).",
        url=public_url,
        skills=SKILLS,
        bearer_token=bearer_token,
        artifact_name="compliance-verdict",
    )


if __name__ == "__main__":
    import os

    from setu_sdk.a2a_serve import run

    port = int(os.environ.get("PORT", "9102"))
    run(build_app(os.environ.get("PUBLIC_URL", f"http://127.0.0.1:{port}")), port=port)
