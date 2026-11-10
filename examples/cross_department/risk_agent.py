"""Risk-Agent: a native A2A agent, written directly against `a2a-sdk` (no Setu SDK).

It scores a claim, then hands a sub-task to the Compliance agent *through Setu's A2A facade*
(`/a2a/{handle}`), never to Compliance directly. It forwards the `traceparent` Setu sent it, so
Setu stitches both hops into one trace, and names the delegation it was given, so Setu can check
that Risk may ask Compliance and on whose authority.
"""

import uuid
from typing import Any

import httpx
from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import a2a_pb2 as pb
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict, ParseDict  # type: ignore[import-untyped]

CARD_PATH = "/.well-known/agent-card.json"


def risk_score(amount: float) -> tuple[int, str]:
    """A deterministic stand-in for a real model: bigger claims score higher."""
    score = min(99, int(amount / 150))
    return score, "high" if score >= 60 else "medium" if score >= 30 else "low"


class RiskExecutor(AgentExecutor):
    def __init__(self, setu_url: str, setu_api_key: str, compliance_handle: str) -> None:
        self.setu_url = setu_url.rstrip("/")
        self.setu_api_key = setu_api_key
        self.compliance_handle = compliance_handle

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.message is not None
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        u = TaskUpdater(event_queue, task.id, task.context_id)
        await u.start_work()
        try:
            result = await self._assess(context, u)
        except Exception as exc:
            await u.failed(u.new_agent_message([new_text_part(f"risk scoring failed: {exc}")]))
            return
        await u.add_artifact(
            [ParseDict({"data": result}, pb.Part())], name="risk-assessment", last_chunk=True
        )
        await u.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Scoring is quick and not interruptible: acknowledge the cancel and stop reporting.
        await TaskUpdater(event_queue, context.task_id or "", context.context_id or "").cancel()

    async def _assess(self, context: RequestContext, u: TaskUpdater) -> dict[str, Any]:
        assert context.message is not None
        meta = MessageToDict(context.message.metadata).get("setu", {})
        data: dict[str, Any] = next(
            (MessageToDict(p)["data"] for p in context.message.parts if p.HasField("data")), {}
        )
        claim, amount = data.get("claim"), float(data.get("amount", 0))
        score, level = risk_score(amount)
        await u.update_status(
            pb.TASK_STATE_WORKING,
            u.new_agent_message(
                [new_text_part(f"scored claim #{claim}: {level} ({score}); asking Compliance")]
            ),
        )
        compliance = await self._ask_compliance(
            room_id=meta["room_id"],
            delegation_id=data["delegation_id"],
            traceparent=(
                context.call_context.state.get("headers", {}) if context.call_context else {}
            ).get("traceparent"),
            claim=claim,
            amount=amount,
        )
        return {"claim": claim, "score": score, "level": level, "compliance": compliance}

    async def _ask_compliance(
        self,
        *,
        room_id: str,
        delegation_id: str,
        traceparent: str | None,
        claim: Any,
        amount: float,
    ) -> dict[str, Any]:
        """A plain A2A 1.0 `SendMessage` to Setu's facade for the Compliance agent."""
        headers = {"X-API-Key": self.setu_api_key, "A2A-Version": "1.0"}
        if traceparent:
            headers["traceparent"] = traceparent  # continue the trace Setu started
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "ROLE_USER",
                    "contextId": room_id,  # a Setu room: Risk and Compliance are both in it
                    "parts": [
                        {"text": f"policy check for claim #{claim}"},
                        {"data": {"claim": claim, "amount": amount}},
                    ],
                    "metadata": {
                        "setu": {"skill": "compliance.check", "delegation_id": delegation_id}
                    },
                }
            },
        }
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(
                f"{self.setu_url}/a2a/{self.compliance_handle}", json=body, headers=headers
            )
        resp.raise_for_status()
        reply = resp.json()
        if "error" in reply:
            raise RuntimeError(f"Setu refused the Compliance call: {reply['error'].get('message')}")
        task = reply["result"]["task"]
        state = task["status"]["state"]
        if state != "TASK_STATE_COMPLETED":
            why = task["status"].get("message", {}).get("parts", [{}])[0].get("text", "")
            raise RuntimeError(f"Compliance did not complete ({state}) {why}".strip())
        out: dict[str, Any] = task["artifacts"][0]["parts"][0]["data"]
        return out


def build_app(public_url: str, executor: RiskExecutor) -> FastAPI:
    """The A2A app. The executor holds the Setu credentials, which a launcher may fill in after
    the agent is registered (an agent's API key exists only once Setu knows the agent)."""
    card = {
        "name": "Risk-Agent",
        "description": "Scores claims for risk and asks Compliance for a policy check.",
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "risk.score",
                "name": "Risk scoring",
                "description": "Score a claim for risk",
                "tags": ["risk", "claims"],
            }
        ],
        "supportedInterfaces": [
            {
                "url": public_url.rstrip("/") + "/rpc",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
    }
    app = FastAPI(title="Risk-Agent")

    @app.get(CARD_PATH)
    async def agent_card() -> dict[str, Any]:
        return card

    handler = DefaultRequestHandlerV2(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=ParseDict(card, pb.AgentCard()),
    )
    for route in create_jsonrpc_routes(handler, rpc_url="/rpc", enable_v0_3_compat=False):
        app.router.routes.append(route)
    return app


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "9101"))
    uvicorn.run(
        build_app(
            os.environ.get("PUBLIC_URL", f"http://127.0.0.1:{port}"),
            RiskExecutor(
                os.environ["SETU_URL"], os.environ["SETU_API_KEY"], os.environ["COMPLIANCE_HANDLE"]
            ),
        ),
        port=port,
        log_level="warning",
    )
