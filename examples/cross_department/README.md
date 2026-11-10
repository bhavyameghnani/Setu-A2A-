# Cross-department demo

The story from the spec (section 2), end to end. Three teams, each with an agent built a
different way, collaborate through Setu without ever talking to each other directly.

| Team | Agent | Built with | How it joins Setu |
|---|---|---|---|
| Operations | Triage-Agent | a LangGraph graph (`triage_agent.py`) | `LangGraphAgent` from `setu_sdk`, connects with an API key |
| Risk | Risk-Agent | plain `a2a-sdk` server (`risk_agent.py`) | registered by Agent Card URL |
| Compliance | Compliance-Agent | a LangGraph graph (`compliance_agent.py`) | served with `setu_sdk.a2a_serve`, registered by Agent Card URL |

## Run it

```bash
make db-create db-migrate
make bootstrap ORG=Acme DEPT="Operations" SLUG=ops EMAIL=admin@acme.test NAME="Ada Admin"
make dev                                  # in one terminal: Setu on :8000

export DEMO_ADMIN_EMAIL=admin@acme.test DEMO_ADMIN_PASSWORD=...   # in another terminal
make demo                                 # add ARGS=--no-approval to skip the sign-off
```

The script starts the Risk and Compliance agents (ports 9101 and 9102), seeds Setu through its
public API (`seed.py`), runs the story, and prints the answer, the trace and each person's inbox.
It can be run repeatedly: each run creates its own people, Triage agent, room and delegation, and
reuses the Risk and Compliance agents if their cards are already registered. Policies are created
once (by name) and then kept, so `--no-approval` only takes effect on a fresh org.

## Or play it yourself in the browser

```bash
make demo ARGS=--interactive     # same world, nothing scripted; prints who is who
cd web && npm run dev            # http://localhost:5173
```

Sign in as each person (each in their own browser profile or a private window) and run the story:

1. **Olga** (Operations): open the room, type `@triage-<tag> Triage claim #123, amount 12500`
   (the `@` picker completes it) and send. A task card appears and waits.
2. **Carl** (Compliance): his inbox shows *Approval needed from Compliance* with Approve and Deny
   buttons. Approve.
3. Back in **Olga's** room the cards go live: Risk completes, Compliance completes, Triage answers.
   *View trace* shows the tree; *Show delegation chain* goes back to Olga. Her inbox says
   *Risk-Agent finished risk.score for Triage-Agent. Result attached.*
4. **Ada** (auditor): the Audit page, and *Verify chain*.

`server/tests/e2e/test_browser_demo.py` does exactly this in a real browser.

## What happens

1. **Seed.** Departments Risk and Compliance next to Operations; Olga (Ops), Rita (Risk),
   Carl (Compliance) and Ada (auditor); the three agents; one cross-department room; policies:
   Ops may ask Risk, Risk may ask Compliance, and **risk scoring needs a Compliance admin's
   approval**; and a delegation from Olga to the Triage agent for `risk.score` and
   `compliance.check`.
2. **Olga asks** Triage in the room: *"Triage claim #123, amount 12500"*.
3. **Triage delegates and asks.** It passes on to Risk only the scope Risk will need
   (`compliance.check`), then sends Risk a task in the room.
4. **The policy holds the task** until a Compliance admin approves. Carl approves it (the script
   stands in for the inbox).
5. **Setu forwards the task to Risk over A2A.** Risk scores the claim, then asks Compliance
   *through Setu's A2A endpoint* (`/a2a/{handle}`), forwarding the `traceparent` Setu gave it.
6. **One trace** follows Triage -> Risk -> Compliance, with each policy decision and the
   delegation chain back to Olga.
7. **Triage answers Olga.** People in all three departments find the story in their inbox, for
   example *"Risk-Agent finished risk.score for Triage-Agent. Result attached."*
8. The **audit log** recorded the decisions and verifies as intact.

`tests/test_demo.py` runs exactly this against a real Setu on real sockets.

## Using the pieces in your own agents

```python
# A LangGraph agent in a room (about 20 lines):
client = SetuClient("http://localhost:8000", api_key="stu_...")
await LangGraphAgent(client, graph).run()

# The same graph as an A2A server that others register by URL:
app = build_a2a_app(
    graph_handler(graph),
    name="My-Agent",
    url="http://localhost:9000",
    skills=[Skill("my.skill", "Does my thing")],
)
run(app, port=9000)
```

Nodes reach Setu through `config["configurable"]["setu"]` (annotate the parameter
`RunnableConfig`, or LangGraph will not pass it).
