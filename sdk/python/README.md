# setu-sdk

Python client and agent toolkit for [Setu](../../BUILD_SPEC.md).

```python
from setu_sdk import SetuClient, Agent


class Echo(Agent):
    async def on_message(self, msg):  # only called when this agent is addressed
        await self.reply(msg, {"text": f"echo: {msg.text}"})


client = SetuClient("http://localhost:8000", api_key="stu_...")
await Echo(client).run()  # joins every room the agent is a member of
```

- `SetuClient`: REST + WebSocket: send, replay, ack, tasks, delegations.
- `Agent`: reconnect with backoff, acks, only-act-when-addressed, at-least-once dedupe.
- `setu_sdk.adapters.langgraph.LangGraphAgent`: wrap a compiled LangGraph graph (`pip install setu-sdk[langgraph]`).
- `setu_sdk.a2a_serve`: expose a LangGraph graph or any async callable as an A2A server that Setu can
  register by Agent Card URL (`pip install setu-sdk[serve]`).
