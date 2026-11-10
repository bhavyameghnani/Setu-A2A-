# Setu — Build Spec (for Claude Code)

> **Status:** every phase in section 15 is built. This spec still defines the product and its rules. Where the build chose differently, or went beyond it (facade persistence, push notifications, signed artifacts, teams, analytics, the redesigned web app), the choice is recorded in [docs/decisions.md](docs/decisions.md). For setup and an overview see [README.md](README.md).

> Name: **Setu** (Hindi for "bridge"). A governed hub where AI agents from different teams (and the humans who own them) collaborate in shared rooms. Agents connect through the **A2A (Agent2Agent) protocol**. Every hop is traced, policy-checked and audited, and humans in each department are told what happened.

You are building this as a solo-developer MVP. Work in small, verifiable increments. After each phase, run the tests and stop for review. Do not build anything listed under Non-goals.

---

## 1. Product in one paragraph

Many teams in an organization already have agents. Setu lets each team **register its existing agent** (any A2A-compliant agent, or a LangGraph agent wrapped by our SDK) and then lets agents collaborate **across departments** in shared **rooms**. Setu is an **A2A hub**: it routes tasks between agents deterministically (no LLM in the path), enforces policy and delegation scope, carries one **trace** across every hop, and posts **activity messages** to the humans of the departments involved, so they can see what their agents did and step in. Humans log in per department and only see what their department is entitled to see.

## 2. The feature story (this is what we demo)

1. The **Operations** team has a claims-triage agent. The **Risk** team has a risk-scoring agent. The **Compliance** team has a policy-check agent. Each already exists and speaks A2A (or is a LangGraph app).
2. Each team registers its agent in Setu by pasting its **Agent Card URL** (or by wrapping a LangGraph agent with the SDK).
3. A user in Operations creates a cross-department room and adds the agents.
4. The triage agent sends a task to the risk agent via the room. Setu forwards it over A2A, streams progress back into the room, and the risk agent hands a sub-task to the compliance agent.
5. Everyone sees one shared **trace**: who asked whom, with what delegated scope, which policy allowed it, what came back.
6. Humans in Operations, Risk and Compliance each get a message in their inbox ("Risk-Agent finished scoring claim #123 for Ops-Agent: result attached"). A compliance rule can pause a step until a human approves.

## 3. Principles

1. **Routing is deterministic.** No LLM in the message path. Ever.
2. **A2A-native.** Setu speaks standard A2A to agents. We do not invent a competing agent protocol.
3. **Bring your own agent.** Registering an existing agent must take minutes and no rewrite.
4. **Governance is the product.** Policy, delegation, trace and audit matter more than the chat UI.
5. **Department-scoped by default.** Visibility and notifications follow department membership.
6. **Boring tech.** Prefer well-known libraries over clever ones.

## 4. Non-goals (do NOT build)

- CrewAI or Claude Code/Claude Agent SDK adapters, and an MCP server (later, not now)
- Multi-region scale, sharding, custom brokers
- Edge/drone deployment, on-prem packaging (design for it, do not build it)
- Billing, SSO/SAML, polished org-admin UI
- LLM-based routing or agent selection
- Mobile apps

## 5. Tech stack (decided)

| Layer | Choice | Why |
|---|---|---|
| Language (server) | Python 3.12 | Agent ecosystem is Python-first; shares models with SDK |
| API framework | FastAPI + Uvicorn | REST + WebSockets + SSE, Pydantic v2 |
| Agent protocol | **A2A** via the official `a2a-sdk` (Python) | Standard interop; see section 8 |
| HTTP client | httpx (async) | A2A client calls and card fetches |
| Database | PostgreSQL 16 | Source of truth; JSONB payloads |
| ORM / migrations | SQLAlchemy 2 (async) + Alembic | Standard, explicit |
| Realtime fan-out | Postgres `LISTEN/NOTIFY` (notify carries only ids, clients fetch rows) + replay from the `messages` table by `seq` | No Redis to run; one datastore. Add Redis later only if fan-out outgrows Postgres |
| Auth | JWT (PyJWT) for humans; hashed API keys (argon2) for agents/services | Short-lived tokens |
| Delegation | RFC 8693-style token exchange, in-app | On-behalf-of chains with narrowing scopes |
| Policy engine | In-process rules behind a `PolicyEngine` interface (OPA/Cedar later) | Simple MVP, swappable |
| Tracing | W3C Trace Context (`traceparent`) + own span table; OpenTelemetry hooks stubbed | One trace across A2A hops |
| Frontend | React 18 + Vite + TypeScript + Tailwind + TanStack Query | Fast to build |
| Setu SDK | Python package `setu-sdk` with a **LangGraph adapter only** | Teams with LangGraph agents |
| Tooling | `uv`, `ruff`, `mypy`, `pytest` + `pytest-asyncio`, Makefile + `scripts/dev.sh` | Reproducible dev without containers |
| Logging | structlog (JSON), trace_id on every line | Debuggable |

If a choice blocks progress, propose an alternative in one paragraph and wait. Do not silently swap the stack.

**Local setup (no Docker).** The only external dependency is PostgreSQL 16, installed locally (`brew install postgresql@16` on macOS, `apt install postgresql` on Linux) or a hosted instance such as Neon (set `DATABASE_URL`). Everything else runs as plain processes via `uv` (server) and `npm` (web). Do not add Docker, Compose, or Redis. Postgres `LISTEN/NOTIFY` is used for fan-out; because `NOTIFY` payloads are limited to about 8 KB, send only ids and have consumers fetch the rows.

## 6. Repo layout

```
setu/
  BUILD_SPEC.md
  CLAUDE.md                  # conventions + pointer (create in Phase 0)
  Makefile                   # dev, test, lint, typecheck, db-create, db-migrate
  scripts/dev.sh             # starts api + web; checks Postgres is reachable
  .env.example               # DATABASE_URL etc.
  server/
    pyproject.toml
    alembic/
    app/
      main.py  config.py
      db/                    # models, session
      api/                   # REST routers: auth, departments, participants, rooms, messages, agents, tasks, notifications, traces, audit
      ws/                    # WebSocket gateway (humans + SDK agents)
      a2a/
        registry.py          # register agent by card URL, validate, refresh card
        client.py            # outbound A2A calls to registered agents
        facade.py            # inbound A2A endpoint per agent: /a2a/{agent_handle}
        mapping.py           # A2A <-> Setu translation (see section 8)
        versions.py          # A2A 1.0 and 0.3 compatibility handling
      core/
        routing.py           # deterministic routing
        lifecycle.py         # message/task status machine
        replay.py            # cursor replay + snapshots
        policy/              # PolicyEngine interface + rules impl
        delegation.py        # token exchange, scope narrowing
        tracing.py           # trace/span creation and propagation
        notifications.py     # deterministic human activity messages
        audit.py             # hash-chained append-only log
        auth.py
      schemas/               # Pydantic contract
    tests/
  sdk/python/setu_sdk/   # client, Agent base, adapters/langgraph.py, a2a_serve.py
  web/src/                   # React app
  examples/
    cross_department/        # Ops, Risk, Compliance demo agents (A2A) + seed script
```

## 7. Data model (Postgres)

- `orgs(id, name)`
- `departments(id, org_id, name, slug)`
- `participants(id, org_id, department_id, kind ['human'|'agent'], display_name, handle, status, created_at)`
- `humans(participant_id, email, password_hash, roles[])`  -- roles: `dept_member`, `dept_admin`, `org_admin`, `auditor`
- `agents(participant_id, owner_department_id, transport ['a2a_remote'|'sdk'], a2a_card_url, a2a_card_json JSONB, a2a_protocol_versions[], skills JSONB, auth_config JSONB (encrypted), card_fetched_at, enabled)`
- `api_keys(id, participant_id, key_hash, scopes[], last_used_at, revoked_at)`
- `rooms(id, org_id, name, topic, visibility ['private'|'department'|'cross_department'], owning_department_id, created_by, created_at)`
- `room_members(room_id, participant_id, role ['owner'|'member'|'observer'], joined_at)`
- `room_department_access(room_id, department_id, access ['read'|'participate'])`  -- humans of that department can see the room
- `messages(id [uuid7], room_id, seq, sender_id, kind ['text'|'task'|'task_update'|'result'|'approval_request'|'approval_response'|'activity'|'system'], body JSONB, mentions uuid[], reply_to, task_id, trace_id, span_id, delegation_chain_id, created_at)`
- `message_status(message_id, participant_id, status, updated_at)`
- `tasks(id, room_id, a2a_task_id, a2a_context_id, requester_id, assignee_id, parent_task_id, state ['submitted'|'working'|'input_required'|'auth_required'|'completed'|'failed'|'canceled'|'rejected'], trace_id, delegation_chain_id, created_at, updated_at, completed_at)`
- `task_artifacts(id, task_id, name, parts JSONB, created_at)`
- `spans(id, trace_id, parent_span_id, kind ['message'|'a2a_call'|'policy'|'delegation'|'approval'], actor_id, task_id, started_at, ended_at, status, attributes JSONB)`
- `notifications(id, recipient_id, department_id, room_id, task_id, trace_id, summary, detail JSONB, read_at, created_at)`
- `cursors(room_id, participant_id, last_seq)`; `room_snapshots(room_id, up_to_seq, summary_text, created_at)` (background job only)
- `policies(id, org_id, name, rule JSONB, enabled)`
- `delegations(id, root_human_id, from_participant_id, to_participant_id, scopes[], parent_id, expires_at)`
- `audit_log(id, org_id, seq, event_type, actor_id, payload JSONB, prev_hash, hash, created_at)`  -- `hash = sha256(prev_hash || canonical_json(event))`

## 8. A2A integration (the core feature)

> A2A is now at version **1.0.0**; many deployed agents still speak **0.3**. The `a2a.proto` in the A2A repo is the normative source of truth. Before coding this section, read the current spec at a2a-protocol.org and the `a2a-sdk` Python docs, then confirm the method names and types below against them. Note that `a2a-sdk` 1.x targets A2A 1.0, and a 0.3 compatibility layer exists. Implement version detection in `a2a/versions.py` and test against both a 1.0 and a 0.3 agent.

### 8.1 Two roles for Setu
1. **A2A client (outbound).** For every `a2a_remote` agent, Setu fetches the agent's **Agent Card** (`/.well-known/agent-card.json` by default), stores it, and calls the agent over its advertised binding (start with JSON-RPC over HTTP with SSE streaming; REST/gRPC later).
2. **A2A server facade (inbound).** Setu exposes each registered agent at `/a2a/{agent_handle}` with its own **proxied Agent Card** (pointing at Setu, not the origin). Agents that want to call another team's agent do so *through Setu* using plain A2A. Policy, delegation and tracing apply to every call, even when agents never open the room UI.

### 8.2 Registering an existing agent
`POST /v1/agents/register {card_url, department_id, auth?}`:
1. Fetch and validate the card. Store skills, supported interfaces, protocol versions and declared security schemes.
2. Store the credentials Setu needs to call that agent (encrypted).
3. Create the agent `participant` owned by the department. Capabilities come from card skills, so `discover` works.
4. Re-fetch cards on a schedule and when a call fails with a card/version mismatch.
Registering must work with a card URL only (plus credentials where the card requires them).

### 8.3 Mapping table (Setu <-> A2A)

| Setu | A2A |
|---|---|
| Room | `contextId` (one room = one context; use the room id or a stable derived id) |
| Task thread | A2A `Task` (`taskId`), mirrored in `tasks` |
| Task message from requester | `SendMessage` / `SendStreamingMessage` (0.3: `message/send`, `message/stream`) with a `Message` of `Part`s |
| `task_update` messages | streamed status/artifact update events |
| `result` messages | `Artifact`s on the Task |
| Task `state` | A2A task states: submitted, working, input-required, auth-required, completed, canceled, failed, rejected |
| `input_required` | creates an `approval_request`/question message addressed to the requester or a human |
| `auth_required` | triggers the delegation/credential flow (section 10) |
| `trace_id` / `span_id` | propagated as W3C `traceparent` header and mirrored in message metadata |
| Cancel | `CancelTask` |

### 8.4 Execution flow (agent A in Ops delegates to agent B in Risk)
1. A posts a `task` message to the room (or calls Setu's A2A facade) addressed to B.
2. Routing resolves the addressee deterministically. Policy evaluates (department rules, topic rules, approval rules). Decision is written to the audit log and as a policy span.
3. Delegation check: B's scope must be a subset of what A holds from the root human.
4. Setu opens an A2A call to B with the room's `contextId` and `traceparent`; creates a `tasks` row and span.
5. B's streamed updates become `task_update` messages and span events in the room. Artifacts become `result` messages.
6. If B needs another team's agent, it calls Setu's A2A facade for C. This creates a child task under the same trace.
7. On terminal state, Setu posts a final message and generates **notifications** (section 12).

### 8.5 Rules
- Never expose an agent's real endpoint or credentials to other agents; calls always go through Setu.
- Idempotency: A2A message ids dedupe retries.
- Timeouts and retries are configurable per agent; a failed call yields a `failed` task state and a visible failure message, never a silent drop.
- Version negotiation per agent; do not assume all agents are on the same A2A version.

## 9. Routing rules (deterministic)

1. A message is visible to room members and to humans of departments listed in `room_department_access`.
2. A participant is **addressed** if it is in `mentions`, it is the assignee of the referenced task, or it holds a matching subscription (kind + skill/capability).
3. Only addressed participants are expected to act. Others receive context.
4. Order by per-room `seq`. Delivery is at-least-once; consumers dedupe by message id.
5. No LLM calls in `core/routing.py`. Add a test asserting that module has no LLM client imports.

## 10. Control plane

**Policy.** `PolicyEngine.evaluate(ctx) -> allow | deny | require_approval` with reason and rule id, on every send and every A2A call. Context: sender, recipient, departments of each, room, message kind, topics/skills, delegation chain, time. Rule types (JSON): `allow_talk {from_department, to_department}`, `deny_talk`, `topic_block`, `require_approval {skill|kind, approver_department, approver_role}`. Defaults: same-department allowed; cross-department **denied until a rule allows it**; cross-org denied.

**Delegation.** A human grants scopes to an agent. When agent A delegates to B the grant must be a subset of A's scopes and expire no later than A's. Every task carries `delegation_chain_id`; `GET /v1/delegations/{id}/chain` returns the path back to the root human.

**Audit.** Append-only, hash-chained, DB trigger blocks UPDATE/DELETE. `POST /v1/audit/verify` finds the first broken link.

## 11. Tracing

- One `trace_id` per root request (a human message or an externally started A2A call). Child tasks and calls create child spans.
- Span kinds: message, a2a_call, policy, delegation, approval. Attributes include department, agent, task state, latency, policy decision.
- `GET /v1/traces/{trace_id}` returns a tree. The trace view shows who asked whom, which policy allowed it, delegated scope, durations and results.
- Respect department visibility: a user sees only the spans for rooms/tasks their department can access; auditors see the full trace.
- Accept an incoming `traceparent` from external A2A callers and continue it.

## 12. Human visibility and notifications (department-scoped)

- Humans log in and belong to a department (and optionally others). Everything they see (rooms, tasks, traces, notifications) is filtered by department access.
- On task lifecycle events, `core/notifications.py` creates **activity messages** deterministically from templates, for the humans of every department involved (requester's, assignee's, approver's): "Risk-Agent finished scoring claim #123 for Ops-Agent. Result attached. [View trace]".
- Events: task started, task completed/failed/canceled, input-required, approval requested/granted/denied, policy denial, agent registered or card changed.
- Delivery: `notifications` rows + a WebSocket push + an inbox in the web app with unread counts. Optional email later.
- A human can open the room from a notification, reply (posts as a human message that agents can address), approve or reject a gated step, or cancel a task.
- Optional later: an LLM-written summary produced by a **background job** and attached to the notification. It must never sit in the routing path.

## 13. SDK (Python) and LangGraph

- `setu_sdk.SetuClient(base_url, api_key)`: join rooms, send, ack, replay, subscribe to tasks.
- `Agent` base class: async `on_message(msg)`, auto-ack lifecycle, reconnect with backoff, cursor persistence, only-act-when-addressed guard.
- `adapters/langgraph.py`: wrap an existing compiled LangGraph graph so it joins rooms in about 20 lines and maps graph runs to Setu tasks.
- `a2a_serve.py`: a helper that exposes a LangGraph (or any async callable) agent as a standard A2A server using `a2a-sdk`, with a proper Agent Card, so a team can then register it by URL. This is the "I have a LangGraph agent, make it A2A" path.
- Do NOT build CrewAI, Claude Agent SDK or MCP integrations now.

## 14. Frontend (web/)

Login (department-aware), Rooms (filtered by department), Room view (stream, participants with department badges, task cards with live state, mentions), **Inbox** (notifications with unread counts, approve/reject inline), **Agent registry** (register by card URL, show card skills, department owner, version, health), **Trace viewer** (tree with delegation chain and policy decisions), Audit view (auditor role), Policies (basic list/editor). Keep it plain and fast.

## 15. Phases and acceptance criteria

**Phase 0: Scaffold.** Layout, `uv` project, Makefile, `scripts/dev.sh`, `.env.example`, `CLAUDE.md`. No Docker anywhere.
*Accept:* with local Postgres running, `make db-create db-migrate dev` serves `/healthz`; `make test` runs against a throwaway test database.

**Phase 1: Core messaging.** Orgs, departments, participants, rooms, members, messages with per-room `seq`, REST + WebSocket, Postgres LISTEN/NOTIFY fan-out.
*Accept:* two clients exchange ordered messages; restart loses nothing.

**Phase 2: Auth, departments, visibility.** Human login with roles, API keys, department access on rooms, filtered queries.
*Accept:* a user in Ops cannot read a Risk-only room; a cross-department room shows only to granted departments.

**Phase 3: Routing, lifecycle, replay.** Mentions, addressing, status machine, cursors, replay, snapshot job.
*Accept:* unaddressed agents do not act; a restarted agent catches up from its cursor.

**Phase 4: A2A client + registry.** Register by Agent Card URL, store card, discovery by skill, outbound `SendMessage` + streaming, task mirroring, 1.0 and 0.3 handling.
*Accept:* register the example Ops and Risk agents by card URL; a room task reaches the Risk agent, streams updates back as messages, and ends with an artifact. Works against both an A2A 1.0 and a 0.3 test agent.

**Phase 5: Control plane.** Policy engine, approvals (`input_required` mapping), delegation chain, audit with verify.
*Accept:* cross-department call is denied until a rule allows it; a `require_approval` rule pauses the task until a human approves; a sub-delegation cannot exceed scope; tampering is detected by verify.

**Phase 6: A2A facade + tracing.** Inbound `/a2a/{handle}` with proxied cards, `traceparent` propagation, spans, trace API.
*Accept:* agent A calls agent B only through Setu's A2A endpoint; one trace shows A -> B -> C with policy and delegation spans; real endpoints are never exposed.

**Phase 7: Human notifications.** Notification generation, WS push, inbox API, reply/approve/cancel actions.
*Accept:* in the cross-department demo, humans in Ops, Risk and Compliance each receive the right activity messages for their department; an approval from a Compliance user resumes the task.

**Phase 8: SDK, LangGraph, demo.** `setu-sdk`, LangGraph adapter, `a2a_serve.py`, `examples/cross_department/` with seed script.
*Accept:* a script seeds three departments, three agents (one native A2A, one via `a2a_serve`, one via the LangGraph adapter) and runs the section 2 story end to end.

**Phase 9: Web UI.** Pages from section 14.
*Accept:* the full demo runs from the browser, including inbox, approval and trace viewer.

## 16. Engineering conventions

- Type everything; `mypy --strict` on `server/app/core` and `server/app/a2a`.
- Pydantic models in `schemas/` are the contract; generate TS types for web from OpenAPI.
- Integration tests against a real local Postgres (pytest fixtures create and drop a throwaway database per session). No Docker, no testcontainers, no mocks for the database. Include contract tests that run Setu against a reference A2A agent built with `a2a-sdk`.
- Forward-only migrations; never edit an applied one.
- Secrets only via env; `.env.example` committed. Agent credentials are encrypted at rest.
- Small commits, conventional messages.
- If a requirement is ambiguous, choose the simplest option, record it in `docs/decisions.md`, and continue. Ask only when a choice is expensive to reverse.

## 17. How to start (first prompt for Claude Code)

> Read BUILD_SPEC.md fully, especially section 8 (A2A). Execute **Phase 0 only**: create the repo layout, `uv` project, Makefile, `scripts/dev.sh`, `.env.example`, and a `CLAUDE.md` summarizing section 16 and pointing to this spec. Run the checks, then stop and summarize what you built and any decisions you made. Do not start Phase 1.

After each phase, review, then say: "Proceed to Phase N."