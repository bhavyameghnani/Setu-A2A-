"""Triage-Agent: a LangGraph app run by `setu_sdk`'s LangGraph adapter.

A human in Operations asks it to triage a claim in the room. The graph parses the request,
asks the Risk agent for a score (a real Setu task that Setu forwards over A2A), and replies.
Three steps; no LLM, so the demo runs offline, but nothing stops a node from calling one.

The adapter is the whole Setu integration: `LangGraphAgent(client, graph).run()`.
"""

import re
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from setu_sdk import SetuClient
from setu_sdk.adapters.langgraph import LangGraphAgent, SetuContext

CLAIM = re.compile(r"claim\s*#?\s*(\d+)", re.I)
AMOUNT = re.compile(r"amount\s*\$?\s*([\d,]+(?:\.\d+)?)", re.I)


@dataclass(frozen=True)
class TriageConfig:
    risk_agent_id: str  # the Risk agent's participant id
    delegation_id: str  # the grant a human gave this agent (what it may ask others to do)
    wait_seconds: float = 120.0  # includes time a human takes to approve


class State(TypedDict, total=False):
    text: str
    claim: int
    amount: float
    risk: dict[str, Any]
    task_state: str
    trace_id: str | None
    output: str


def build_graph(cfg: TriageConfig) -> Any:
    def parse(state: State) -> State:
        text = state["text"]
        claim, amount = CLAIM.search(text), AMOUNT.search(text)
        if not claim or not amount:
            raise ValueError("say which claim and amount, e.g. 'Triage claim #123, amount 12500'")
        return {"claim": int(claim.group(1)), "amount": float(amount.group(1).replace(",", ""))}

    async def ask_risk(state: State, config: RunnableConfig) -> State:
        setu: SetuContext = config["configurable"]["setu"]
        c = setu.client
        # Pass on only what Risk needs: it may ask Compliance, and nothing else.
        sub = await c.delegate(
            cfg.risk_agent_id, ["skill:compliance.check"], parent_id=cfg.delegation_id
        )
        msg = await c.send_task(
            setu.room_id,
            cfg.risk_agent_id,
            text=f"score claim #{state['claim']}",
            data={"claim": state["claim"], "amount": state["amount"], "delegation_id": sub["id"]},
            skill="risk.score",
            delegation_id=cfg.delegation_id,
        )
        task = await c.task_for(msg)
        final = await c.wait_task(task["id"], max_wait=cfg.wait_seconds)  # may wait on a human
        out: State = {"task_state": final["state"], "trace_id": msg.trace_id}
        if final["state"] == "completed":
            out["risk"] = final["artifacts"][0]["parts"][0]["data"]
        return out

    def summarize(state: State) -> State:
        claim, trace = state["claim"], state.get("trace_id")
        # Data parts travel as protobuf Structs, which turns every number into a float.
        if state["task_state"] != "completed":
            ended = state["task_state"]
            return {"output": f"Claim #{claim}: the risk step ended {ended}. Trace: {trace}"}
        r = state["risk"]
        c = r["compliance"]
        verdict = (
            "needs compliance review"
            if c["verdict"] == "review_required"
            else "cleared by compliance"
        )
        return {
            "output": (
                f"Claim #{claim}: risk {r['level'].upper()} ({int(r['score'])}); "
                f"{verdict} ({c['reason']}). Trace: {trace}"
            )
        }

    g = StateGraph(State)
    g.add_node("parse", parse)
    g.add_node("ask_risk", ask_risk)
    g.add_node("summarize", summarize)
    g.add_edge(START, "parse")
    g.add_edge("parse", "ask_risk")
    g.add_edge("ask_risk", "summarize")
    g.add_edge("summarize", END)
    return g.compile()


def build_agent(client: SetuClient, cfg: TriageConfig, **kwargs: Any) -> LangGraphAgent:
    return LangGraphAgent(client, build_graph(cfg), **kwargs)


if __name__ == "__main__":
    import asyncio
    import os

    async def main() -> None:
        async with SetuClient(os.environ["SETU_URL"], os.environ["SETU_API_KEY"]) as client:
            cfg = TriageConfig(os.environ["RISK_AGENT_ID"], os.environ["DELEGATION_ID"])
            await build_agent(client, cfg).run()

    asyncio.run(main())
