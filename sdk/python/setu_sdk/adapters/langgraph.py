"""Run a compiled LangGraph graph as a Setu agent.

    from setu_sdk import SetuClient
    from setu_sdk.adapters.langgraph import LangGraphAgent

    client = SetuClient("http://localhost:8000", api_key="stu_...")
    agent = LangGraphAgent(client, graph)          # `graph` is your compiled StateGraph
    await agent.run()                              # joins every room the agent is a member of

Every message addressed to the agent is one graph *run*. The run is keyed to the message
(`thread_id` = the message id, so a checkpointer keeps one thread per request), and its progress
and result are posted back into the room as `task_update` / `result` messages that answer the
request (`reply_to`), addressed to whoever asked.

Your graph receives the request as input (see `to_input`) and can reach Setu from any node:

    async def my_node(state, config: RunnableConfig):   # LangGraph passes `config` only when
        setu = config["configurable"]["setu"]           # it is annotated `RunnableConfig`
        await setu.client.send_task(setu.room_id, risk_agent_id, skill="risk.score", ...)

This module does not import LangGraph: it only calls `graph.ainvoke` / `graph.astream`, so it
works with any object that has them.

Note on tasks: a graph run is a *message-level* task. Setu creates `tasks` rows (with state
tracking, notifications and traces) for tasks it sends to registered A2A agents; for an SDK agent
like this one the room messages are the record. To get the full task machinery for a graph,
expose it with `setu_sdk.a2a_serve` and register it by Agent Card URL instead.
"""

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from setu_sdk.agent import Agent
from setu_sdk.client import SetuClient
from setu_sdk.models import Message

log = logging.getLogger("setu_sdk.langgraph")

InputFn = Callable[[Message], Mapping[str, Any]]
OutputFn = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class SetuContext:
    """Handed to graph nodes as `config["configurable"]["setu"]`."""

    client: SetuClient
    message: Message
    agent_id: str

    @property
    def room_id(self) -> str:
        return self.message.room_id


def default_input(msg: Message) -> Mapping[str, Any]:
    """The request as graph input: `text`, `data`, `skill` and the whole `message` (a dict)."""
    return {"text": msg.text, "data": msg.data, "skill": msg.skill, "message": msg.raw}


def default_output(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """The reply body from the final graph state: its `output` (or `result`, `text`) key, a str
    or a dict; failing that, the final state itself as data."""
    for key in ("output", "result", "text"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return {"text": value}
        if isinstance(value, Mapping):
            return {
                "data": dict(value),
                **({"text": str(value["text"])} if "text" in value else {}),
            }
    return {"data": {k: v for k, v in state.items() if k != "message"}}


class LangGraphAgent(Agent):
    def __init__(
        self,
        client: SetuClient,
        graph: Any,
        *,
        to_input: InputFn = default_input,
        to_output: OutputFn = default_output,
        kinds: Collection[str] = ("text", "task"),
        progress: bool = False,
        recursion_limit: int | None = None,
        on_error: Callable[[Message, Exception], Awaitable[None]] | None = None,
        **agent_kwargs: Any,
    ) -> None:
        """`kinds` are the message kinds that start a run. Setu also addresses an agent with the
        replies to tasks it sent (`task_update`, `result`, `approval_*`); those are not requests,
        so they are skipped by default. `progress=True` posts a `task_update` after each graph
        step (streams `updates`)."""
        super().__init__(client, **agent_kwargs)
        self._kinds = frozenset(kinds)
        self.graph = graph
        self._to_input = to_input
        self._to_output = to_output
        self._progress = progress
        self._recursion_limit = recursion_limit
        self._on_error = on_error

    async def on_message(self, msg: Message) -> None:
        if msg.kind not in self._kinds:
            return
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": msg.id,
                "setu": SetuContext(self.client, msg, self.me),
            }
        }
        if self._recursion_limit:
            config["recursion_limit"] = self._recursion_limit
        # A request that is a `task` is answered with task_update/result messages; a plain
        # text message is answered with plain text.
        as_task = msg.kind == "task"
        if as_task:
            await self._update(msg, "working")
        try:
            state = await self._run(msg, self._to_input(msg), config)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if as_task:
                await self._update(msg, "failed", error=reason)
            else:  # someone is waiting for an answer to a plain message: tell them
                await self.reply(msg, {"text": f"I could not do that. {reason}"})
            if self._on_error is not None:
                await self._on_error(msg, exc)
            raise
        body = {**self._to_output(state), "request_message_id": msg.id}
        if as_task:
            await self.reply(msg, body, kind="result")
            await self._update(msg, "completed")
        else:
            await self.reply(msg, body)

    async def _run(
        self, msg: Message, graph_input: Mapping[str, Any], config: dict[str, Any]
    ) -> Mapping[str, Any]:
        if not self._progress:
            out: Mapping[str, Any] = await self.graph.ainvoke(dict(graph_input), config)
            return out
        state: dict[str, Any] = dict(graph_input)
        async for update in self.graph.astream(dict(graph_input), config, stream_mode="updates"):
            for node, delta in update.items():
                if isinstance(delta, Mapping):
                    state.update(delta)
                await self._update(msg, "working", text=f"step: {node}")
        return state

    async def _update(
        self, msg: Message, state: str, *, text: str | None = None, error: str | None = None
    ) -> None:
        body: dict[str, Any] = {"state": state, "origin": "sdk", "request_message_id": msg.id}
        if text:
            body["text"] = text
        if error:
            body["error"] = error
        try:
            await self.reply(msg, body, kind="task_update")
        except Exception as exc:  # a failed progress note must not fail the run
            log.warning("could not post task_update: %s", exc)
