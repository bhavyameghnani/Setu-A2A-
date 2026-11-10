# Setu web app

React 18 + Vite + TypeScript + Tailwind + TanStack Query. Plain and fast: no component library, no
router dependency (hash routes), one fetch wrapper.

```bash
make dev                 # API on :8000 and this app on :5173 (npm install runs on first start)
cd web && npm test       # unit tests for the logic that is not React (router, mentions, approvals, ...)
cd web && npm run build  # type-check + production build
make api-types           # regenerate src/api/schema.d.ts from the server's OpenAPI document
```

The browser only talks to `/v1/...` on its own origin; Vite's dev/preview proxy forwards that (and
WebSockets) to `SETU_API` (default `http://localhost:8000`), so the server needs no CORS settings.

## Pages

| Route | What it is |
|---|---|
| `#/rooms` | Rooms your department may see; create one |
| `#/rooms/:id` | Live message stream, `@mention` composer (send as a message or as a task), task cards with live state, artifacts, cancel and inline approval, participants with department badges, add people, let a department in |
| `#/inbox` | Notifications with unread counts and live push; approve or deny inline, reply, cancel the task, open the room, view the trace |
| `#/agents` | Registry: skills, owner department, A2A version, health; register by Agent Card URL; refresh or disable (department admins) |
| `#/traces/:id` | The trace as a tree: who asked whom, the policy decision, the delegation chain back to the person, approvals, durations |
| `#/audit` | The hash-chained audit log, and "Verify chain" (auditors and org admins) |
| `#/policies` | Rules in words; add, switch off, edit as JSON, delete (org admins; auditors read) |

Everything is filtered by what the signed-in person's department may see: the server decides, and
the UI only shows what it was given. A 401 signs you out; a room you cannot see is "not found".

## Layout

```
src/api/      client.ts (fetch + token), socket.ts (reconnecting WebSocket), hooks.ts, types.ts,
              schema.d.ts (generated; do not edit)
src/lib/      pure logic with tests: router, mentions, approvals, policy wording, formatting
src/pages/    one file per page
src/components/  small shared pieces (chips, badges, buttons, the approve/deny box)
```

## Browser tests

`server/tests/e2e/test_browser_demo.py` drives this app in real Chromium (Playwright) against a live
Setu with the demo agents running: three people sign in and play the whole story. Run
`cd server && uv run playwright install chromium` once; `SKIP_E2E=1` skips them,
`E2E_SCREENSHOTS=dir` keeps a screenshot of each step, `E2E_UNMINIFIED=1` builds readable stack traces.
