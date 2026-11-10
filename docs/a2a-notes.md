# A2A research notes (for Setu `a2a/`)

Research notes from when the A2A client and facade were built, kept as a reference for the 1.0 and 0.3 wire formats. The decisions that came out of them are in [decisions.md](decisions.md).

Legend: [T] = tested by me against installed code; [S] = from spec page https://a2a-protocol.org/latest/specification/ (only first ~100k of 256k chars read); [G] = guess/not verified.

## 1. a2a-sdk
- PyPI latest: **a2a-sdk 1.3.0** [T]. Spec latest released: **1.0.0**; previous 0.3.0, 0.2.6, 0.1.0 [S].
- Deps: google-api-core, googleapis-common-protos, httpx, json-rpc, packaging, protobuf, pydantic. Extras: `http-server` (starlette+sse-starlette), `fastapi`, `grpc`, `sql`/db, `encryption`, `all`. Server needs `a2a-sdk[fastapi]` (or `http-server`).
- **Types are protobuf messages** (`a2a.types.a2a_pb2`: `AgentCard, Message, Part, Task, TaskState, Role, SendMessageRequest, StreamResponse, ...`), NOT pydantic. Convert with `google.protobuf.json_format.MessageToDict/ParseDict`. Field names snake_case in Python, camelCase on wire.
- **Ships a 0.3 compat layer** in `a2a.compat.v0_3` (client transports jsonrpc/rest/grpc, server `JSONRPC03Adapter`, `types` pydantic 0.3 models, conversions) [T].
  - Client: `ClientFactory` picks `CompatJsonRpcTransport` automatically when the card interface `protocol_version` is in [0.3, 1.0) (`a2a.compat.v0_3.versions.is_legacy_version`).
  - Server: `create_jsonrpc_routes(handler, rpc_url, enable_v0_3_compat=True)` serves 1.0 and 0.3 on the same endpoint. Dispatch: 1.0 requests carry header `A2A-Version: 1.0`; a request without it is treated as 0.3 and uses 0.3 method names [T]. A 1.0 method name without the header -> error -32009 VERSION_NOT_SUPPORTED [T].
- Module layout [T]: `a2a.client` (`ClientFactory, ClientConfig, create_client, A2ACardResolver, card_resolver.parse_agent_card, interceptors, auth`), `a2a.server.routes` (`create_agent_card_routes, create_jsonrpc_routes, create_rest_routes, add_a2a_routes_to_fastapi`), `a2a.server.request_handlers.DefaultRequestHandlerV2`, `a2a.server.agent_execution.{AgentExecutor,RequestContext}`, `a2a.server.tasks.{InMemoryTaskStore,TaskUpdater}`, `a2a.helpers.proto_helpers` (`new_task_from_user_message,new_text_part,...`), `a2a.utils.constants` (`AGENT_CARD_WELL_KNOWN_PATH='/.well-known/agent-card.json'`, `VERSION_HEADER='A2A-Version'`).
- Client usage [T]:
```python
card = await A2ACardResolver(httpx_client, base_url, agent_card_path=...).get_agent_card()  # handles 0.3 cards
client = ClientFactory(ClientConfig(httpx_client=h, streaming=True)).create(card)
req = SendMessageRequest(message=Message(message_id=..., role=Role.ROLE_USER, parts=[Part(text="hi")]))
async for ev in client.send_message(req): ...   # StreamResponse oneof: task | message | status_update | artifact_update
```
  Works against both the 1.0 and 0.3 server below, in-process via `httpx.ASGITransport` [T]. Client also has get_task/cancel_task/subscribe/list_tasks. Note `ClientFactory` sets `A2A-Version: 1.0` default header on the shared httpx client (it mutates the client you pass in!).

## 2. Agent Card
| | 1.0 | 0.3 |
|---|---|---|
| path | `/.well-known/agent-card.json` | `/.well-known/agent.json` (older), many 0.3.x agents already serve `agent-card.json` [G: try card.json first, fall back to agent.json] |
| endpoint | `supportedInterfaces: [{url, protocolBinding:"JSONRPC"\|"GRPC"\|"HTTP+JSON", protocolVersion:"1.0", tenant?}]` | top-level `url`, `preferredTransport`, `additionalInterfaces:[{url,transport}]`, `protocolVersion:"0.3.0"` |
| security | `securitySchemes: {name:{apiKeySecurityScheme:{location,name}} / {httpAuthSecurityScheme:{scheme,bearerFormat}} / oauth2SecurityScheme / openIdConnectSecurityScheme / mtlsSecurityScheme}`, `securityRequirements:[{schemes:{name:{list:[scopes]}}}]` | `securitySchemes:{name:{type:"apiKey",in:"header",name:"X-API-Key"}\|{type:"http",scheme:"bearer"}}`, `security:[{name:[scopes]}]` |
| ext card | `capabilities.extendedAgentCard` | `supportsAuthenticatedExtendedCard` |
| same | `name, description, version, skills[{id,name,description,tags,examples,inputModes,outputModes}], capabilities.streaming/pushNotifications, defaultInput/OutputModes, provider, signatures` | same |

Version-detection heuristic [T-backed by SDK's own logic]:
```python
def detect(card: dict) -> str:
    ifaces = card.get("supportedInterfaces")
    if ifaces:                       # 1.0 shape; per-interface version, pick JSONRPC iface
        j = next((i for i in ifaces if i.get("protocolBinding") == "JSONRPC"), ifaces[0])
        return j.get("protocolVersion", "1.0")      # may be "0.3" etc.
    return card.get("protocolVersion", "0.3.0")     # legacy: top-level url (+ protocolVersion)
# major = 1 -> 1.0 wire (PascalCase methods, A2A-Version: 1.0); 0.x -> 0.3 wire
```
Gotcha: an agent can advertise several interfaces with different versions; choose per-interface. SDK 1.3 `parse_agent_card()` normalises 0.3 cards into 1.0 proto (url -> supportedInterfaces, default version "0.3.0", legacy security -> new shape); reuse it or copy its logic. Verified 1.0 card JSON from SDK (no `protocolVersion` top-level, no `kind`):
`{"name":"Echo","supportedInterfaces":[{"url":"http://testserver/rpc","protocolBinding":"JSONRPC","protocolVersion":"1.0"}],"version":"1.0.0","capabilities":{"streaming":true},"defaultInputModes":["text/plain"],"defaultOutputModes":["text/plain"],"skills":[...]}`

## 3. Methods and wire shapes
Methods (verified in SDK dispatchers [T]; 1.0 list also in spec 9.4 [S]):
| op | 0.3 | 1.0 |
|---|---|---|
| send | `message/send` | `SendMessage` |
| stream | `message/stream` | `SendStreamingMessage` |
| get | `tasks/get` | `GetTask` (params `{id, historyLength?}`) |
| cancel | `tasks/cancel` | `CancelTask` (`{id}`) |
| resubscribe | `tasks/resubscribe` | `SubscribeToTask` |
| list | - | `ListTasks` |
| push cfg | `tasks/pushNotificationConfig/{set,get,list,delete}` | `{Create,Get,List,Delete}TaskPushNotificationConfig` |
| ext card | `agent/getAuthenticatedExtendedCard` | `GetExtendedAgentCard` |
Header: 1.0 clients MUST send `A2A-Version: 1.0` [S]; the SDK server treats missing header as 0.3 [T]. Error for unknown task: -32001 "Task not found" (both) [T].

### 1.0 (captured from SDK server) [T]
Enums are proto names: `ROLE_USER|ROLE_AGENT`; `TASK_STATE_{SUBMITTED,WORKING,COMPLETED,FAILED,CANCELED,INPUT_REQUIRED,REJECTED,AUTH_REQUIRED}` (+UNSPECIFIED). No `kind` discriminators; Part is `{"text":..}` / `{"raw":b64,"filename","mediaType"}` / `{"url":..}` / `{"data":{..}}`. Results/stream events are wrapped in a oneof key. No `final` field on status updates; the stream ends after the terminal state.
```json
// request  (header A2A-Version: 1.0)
{"jsonrpc":"2.0","id":1,"method":"SendStreamingMessage","params":{"message":{"messageId":"m2","role":"ROLE_USER","parts":[{"text":"hi"}],"contextId":"<optional>","taskId":"<optional, to continue>"},"configuration":{"returnImmediately":false}, "metadata":{}}}
// SSE (Content-Type text/event-stream), each line `data: <json>`:
{"result":{"task":{"id":"T","contextId":"C","status":{"state":"TASK_STATE_SUBMITTED"},"history":[{"messageId":"m2","contextId":"C","taskId":"T","role":"ROLE_USER","parts":[{"text":"hi"}]}]}},"id":1,"jsonrpc":"2.0"}
{"result":{"statusUpdate":{"taskId":"T","contextId":"C","status":{"state":"TASK_STATE_WORKING","timestamp":"2026-11-05T21:56:54.941736Z"}}},"id":1,"jsonrpc":"2.0"}
{"result":{"artifactUpdate":{"taskId":"T","contextId":"C","artifact":{"artifactId":"A","name":"result","parts":[{"text":"echo: hi"}]},"lastChunk":true}},"id":1,"jsonrpc":"2.0"}
{"result":{"statusUpdate":{"taskId":"T","contextId":"C","status":{"state":"TASK_STATE_COMPLETED","timestamp":"..."}}},"id":1,"jsonrpc":"2.0"}
// non-stream SendMessage result (final task):
{"result":{"task":{"id":"T","contextId":"C","status":{"state":"TASK_STATE_COMPLETED","timestamp":"..."},"artifacts":[{"artifactId":"A","name":"result","parts":[{"text":"echo: hi"}]}],"history":[...]}},"id":1,"jsonrpc":"2.0"}
// a direct reply instead of a task would be {"result":{"message":{...}}}
```
Note: proto3 JSON omits default values, so absent `lastChunk`/`append` = false. Interrupted states (INPUT_REQUIRED, AUTH_REQUIRED) are terminal for that stream; client sends a new message with the same `taskId`+`contextId`.

### 0.3 (captured from SDK compat path and my hand-rolled server) [T]
Lowercase-hyphen states: `submitted, working, input-required, auth-required, completed, canceled, failed, rejected, unknown`. Role `user|agent`. `kind` discriminators everywhere; `final` on status-update; result NOT wrapped.
```json
{"jsonrpc":"2.0","id":1,"method":"message/stream","params":{"message":{"kind":"message","messageId":"m2","role":"user","parts":[{"kind":"text","text":"hi"}],"contextId":"C?","taskId":"T?"}}}
{"id":1,"jsonrpc":"2.0","result":{"kind":"task","id":"T","contextId":"C","status":{"state":"submitted"},"history":[{"kind":"message","messageId":"m2","role":"user","taskId":"T","contextId":"C","parts":[{"kind":"text","text":"hi"}]}]}}
{"id":1,"jsonrpc":"2.0","result":{"kind":"status-update","taskId":"T","contextId":"C","final":false,"status":{"state":"working","timestamp":"..."}}}
{"id":1,"jsonrpc":"2.0","result":{"kind":"artifact-update","taskId":"T","contextId":"C","append":false,"lastChunk":true,"artifact":{"artifactId":"A","name":"result","parts":[{"kind":"text","text":"echo: hi"}]}}}
{"id":1,"jsonrpc":"2.0","result":{"kind":"status-update","taskId":"T","contextId":"C","final":true,"status":{"state":"completed","timestamp":"..."}}}
// message/send result = the Task itself: {"kind":"task","id":"T","contextId":"C","status":{"state":"completed"},"artifacts":[...],"history":[...]}
```
IDs: `messageId` is client-generated (use as idempotency key). `contextId` = grouping (Setu room); client may supply it on the first message, server otherwise generates one and returns it. `taskId` is server-generated; client supplies it only to continue an existing (interrupted) task. Both versions.
Fidelity tip: Setu should normalise internally to one model (suggest 1.0 shape) with state map `input-required <-> TASK_STATE_INPUT_REQUIRED` etc. (`status.state` lower-hyphen <-> upper-snake with TASK_STATE_ prefix).

## 4. Auth and tracing
- Cards declare schemes (see table). Client sends credentials as normal HTTP headers on the JSON-RPC POST (and card fetch if the card is protected): bearer -> `Authorization: Bearer <tok>` [S example]; apiKey with `in/location: header` -> `<name>: <key>` (e.g. `X-API-Key`); query/cookie locations are possible [G: rare]. Spec 3.1.11: client MUST authenticate with one of the card's declared schemes [S]. The SDK has `AuthInterceptor` + `CredentialService` (sets `Authorization` via `context.service_parameters`) [T: exists; not exercised].
- Setu: store per-agent `{scheme_name, kind, header_name, secret}` encrypted; build header from the card scheme at call time.
- Tracing: the spec does not mention `traceparent` (searched the part I read) [S]. A2A over HTTP is plain HTTP, so sending `traceparent: 00-<32hex>-<16hex>-01` as an ordinary header is fine and is what OTel does [G, standard practice]; also mirror in `params.message.metadata.traceparent` (metadata is free-form on Message/SendMessageRequest) for non-HTTP-aware agents. The SDK's own server does not read traceparent (utils/telemetry is OTel instrumentation only) [T: grep found nothing].

## 5. Reference servers (for pytest)
### 1.0 via a2a-sdk (tested, also serves 0.3 on same route) [T]
```python
import asyncio
from fastapi import FastAPI
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes, add_a2a_routes_to_fastapi
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.helpers.proto_helpers import new_task_from_user_message, new_text_part
from a2a.types.a2a_pb2 import AgentCard, AgentCapabilities, AgentInterface, AgentSkill

class EchoExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)          # first event must be the Task (-> SUBMITTED)
        u = TaskUpdater(event_queue, task.id, task.context_id)
        await u.start_work()                                # statusUpdate WORKING
        await asyncio.sleep(0.05)
        await u.add_artifact([new_text_part("echo: " + context.get_user_input())], name="result", last_chunk=True)
        await u.complete()                                  # statusUpdate COMPLETED
    async def cancel(self, context, event_queue):
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

def build_app(base_url="http://testserver", compat03=True) -> FastAPI:
    card = AgentCard(name="Echo", description="echo", version="1.0.0",
        supported_interfaces=[AgentInterface(url=base_url + "/rpc", protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        skills=[AgentSkill(id="echo", name="Echo", description="echoes", tags=["echo"])])
    handler = DefaultRequestHandlerV2(agent_executor=EchoExecutor(), task_store=InMemoryTaskStore(), agent_card=card)
    app = FastAPI()
    add_a2a_routes_to_fastapi(app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/rpc", enable_v0_3_compat=compat03))
    return app
# test: httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app()), base_url="http://testserver")
# SSE with ASGITransport works (c.stream(...).aiter_lines()) [T]. For setu's outbound calls to hit it, inject that httpx client (or use uvicorn on port 0 and put the real URL in the card).
```
### 0.3 pure agent
Not required: the same app with `compat03=True` answers real 0.3 requests [T], but its card says 1.0. For a faithful 0.3 *agent* fixture (legacy card with top-level `url`/`protocolVersion`, no `A2A-Version` handling) use the hand-rolled one [T], file `.../scratchpad/ref_v03.py` (~45 lines): FastAPI with `GET /.well-known/agent-card.json` and `/.well-known/agent.json` returning
`{"protocolVersion":"0.3.0","name":..,"url":base+"/rpc","preferredTransport":"JSONRPC","capabilities":{"streaming":true},"skills":[..],...}`, and `POST /rpc` handling `message/send` (returns Task), `message/stream` (StreamingResponse `text/event-stream`, lines `data: {"jsonrpc":"2.0","id":rid,"result":<event>}\n\n` for task -> status-update(working, final:false) -> artifact-update(lastChunk:true) -> status-update(completed, final:true)), `tasks/get` (-32001 if unknown), else -32601. Verified emits task/status-update/artifact-update/status-update.

## 6. Recommendation
**Use plain httpx with a thin own JSON-RPC/SSE layer for the Setu client; use a2a-sdk only for the test fixtures (reference 1.0 server) and optionally `parse_agent_card`-style card normalisation (copy the logic).** Reasons:
- SDK types are generated protobuf: not pydantic, no static typing story for FastAPI/Pydantic models in Setu, conversions via `MessageToDict` everywhere; the SDK is also fast-moving (1.x with `DefaultRequestHandlerV2` beside a `LegacyRequestHandler`; docstring in AgentExecutor mentions `TASK_STATE_ERROR` that isn't in the enum - doc drift).
- Setu needs control the SDK doesn't give cleanly: per-agent version negotiation, per-call `traceparent`, auth headers derived from card schemes, timeouts/retries, raw event persistence into room messages/spans, idempotent messageId, and to mirror events into its own model. The wire protocol is tiny (4 methods x 2 versions, shapes above) and trivial to cover with a version adapter (`a2a/versions.py`: `detect()`, `encode_send(version, ...)`, `decode_event(version, json) -> normalised event`).
- `ClientFactory` mutates the shared httpx client's default headers (adds `A2A-Version`), picks transport itself, and hides raw frames; harder to test and observe.
- Counterpoint: SDK client already handles both versions correctly in my test, so it is a viable fallback if hand-rolled 1.0 REST/gRPC bindings are wanted later.
- Setu's inbound facade can reuse `a2a-sdk` server pieces (`create_jsonrpc_routes(..., enable_v0_3_compat=True)` gives dual-version serving for free) with a custom `RequestHandler`/`AgentExecutor` that forwards to the origin through the thin client. [G: not tested as a proxying handler.]
- Test strategy: parametrise tests over (1.0 sdk-server, 0.3 hand-rolled server) via `httpx.ASGITransport`.

## Facade (Phase 6) notes
- `DefaultRequestHandlerV2` + `create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True)` mounted in a throwaway `Starlette` app per request, with `scope["path"]="/"`. The handler holds the executor and the task store, so it must outlive the request for background tasks and cancels to work (see decision 82).
- The first event enqueued by an executor must be a `Task` whose `id` equals `context.task_id` (the SDK's `TaskManager` rejects a different id). `TaskUpdater` events then follow.
- `EventQueue` is imported from `a2a.server.events.event_queue` for typing (the `_v2` module does not re-export it for mypy).
- Request-specific data (caller, `traceparent`) reaches the cached executor through `ContextVar`s set before the SDK is invoked; the SDK's tasks copy the context at creation.
- `returnImmediately: true` (1.0) returns the first task snapshot while the executor keeps running.
