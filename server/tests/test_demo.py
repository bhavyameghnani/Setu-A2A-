"""Phase 8 acceptance: the script seeds three departments and three agents (one native A2A, one
served with `a2a_serve`, one run by the LangGraph adapter) and runs the section 2 story end to
end, over real sockets, through Setu."""

import asyncio
from typing import Any

import pytest
from cross_department import run_demo

from tests.conftest import LiveHub
from tests.helpers import PASSWORD, Acme
from tests.sdk_support import live_acme


@pytest.fixture
def acme(live_hub: LiveHub) -> Acme:
    return live_acme(live_hub.url, live_hub.database_url)


def flat(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [x for n in nodes for x in (n, *flat(n["children"]))]


async def story(acme: Acme, hub: LiveHub, **kw: Any) -> run_demo.Story:
    return await asyncio.wait_for(
        run_demo.run(
            hub.url, f"admin-{acme.tag}@acme.test", PASSWORD, quiet=True, max_wait=90, **kw
        ),
        timeout=150,
    )


async def test_the_section_2_story_runs_end_to_end(acme: Acme, live_hub: LiveHub) -> None:
    st = await story(acme, live_hub)
    s = st.seeded

    # 1. Olga got a real answer from the LangGraph agent, built from Risk's and Compliance's work.
    assert st.reply.startswith(
        "Claim #123: risk HIGH (83); needs compliance review "
        "(amount 12,500 is over 10,000). Trace: "
    )
    assert st.reply.endswith(st.trace_id)

    # 2. Two tasks, both done: Triage -> Risk, and Risk -> Compliance. The second one's requester
    #    is the Risk agent, so Risk reached Compliance through Setu's A2A endpoint, not directly.
    assert st.task_states == ["completed", "completed"]
    admin = acme.admin
    tasks = sorted(
        acme.get(f"/v1/rooms/{s.room_id}/tasks", acme.login(s.emails["olga"], s.password)).json(),
        key=lambda t: t["created_at"],
    )
    assert (tasks[0]["requester_id"], tasks[0]["assignee_id"]) == (s.triage["id"], s.risk["id"])
    assert (tasks[1]["requester_id"], tasks[1]["assignee_id"]) == (
        s.risk["id"],
        s.compliance["id"],
    )
    assert tasks[0]["trace_id"] == tasks[1]["trace_id"] == st.trace_id  # one trace across hops
    assert tasks[1]["parent_task_id"] is None and tasks[1]["delegation_chain_id"]
    assert admin is not None

    # 3. One trace shows who asked whom, which policy allowed it, and the delegated scope.
    spans = flat(st.trace["roots"])
    kinds = sorted(x["kind"] for x in spans)
    assert (
        kinds
        == ["a2a_call"] * 2 + ["approval"] + ["delegation"] * 2 + ["message"] * 2 + ["policy"] * 2
    )
    assert len(st.trace["roots"]) == 1 and st.trace["hidden_spans"] == 0
    decisions = [x["attributes"]["decision"] for x in spans if x["kind"] == "policy"]
    assert sorted(decisions) == ["allow", "require_approval"]
    chains = sorted(len(x["attributes"]["chain"]) for x in spans if x["kind"] == "delegation")
    assert chains == [1, 2]  # Triage holds Olga's grant; Risk holds what Triage gave it
    assert {x["attributes"]["root_human"] for x in spans if x["kind"] == "delegation"} == {
        next(x for x in spans if x["kind"] == "delegation")["attributes"]["root_human"]
    }
    approval = next(x for x in spans if x["kind"] == "approval")
    assert approval["status"] == "approved"
    calls = [x for x in spans if x["kind"] == "a2a_call"]
    assert all(c["status"] == "completed" and c["duration_ms"] is not None for c in calls)

    # 4. A Compliance admin approved the risk step; nothing else needed a human.
    assert st.approvals == 1

    # 5. People in each department hear about what involves them. Ops asked, so it hears about
    #    the Triage -> Risk task; Risk is in both tasks; Compliance gave sign-off on the first
    #    and did the second.
    completed = {w: st.events[w].count("task.completed") for w in ("olga", "rita", "carl")}
    assert completed == {"olga": 1, "rita": 2, "carl": 2}
    for who in ("olga", "rita", "carl"):
        assert "approval.requested" in st.events[who], who
    assert "approval.granted" in st.events["olga"] and "approval.granted" in st.events["rita"]
    assert "approval.granted" not in st.events["carl"]  # she made the decision herself
    assert "Risk-Agent finished risk.score for Triage-Agent. Result attached." in st.inboxes["olga"]
    assert (
        "Compliance-Agent finished compliance.check for Risk-Agent. Result attached."
        in st.inboxes["carl"]
    )
    assert any(x.startswith("Approval needed from Compliance:") for x in st.inboxes["rita"])

    # 6. Everything above is in a tamper-evident audit log.
    assert st.audit_ok


async def test_the_story_without_the_approval_gate(acme: Acme, live_hub: LiveHub) -> None:
    st = await story(acme, live_hub, require_approval=False, request="Triage claim #7, amount 900")
    assert st.reply.startswith(
        "Claim #7: risk LOW (6); cleared by compliance (within policy). Trace:"
    )
    assert st.approvals == 0 and st.task_states == ["completed", "completed"]
    assert "approval.requested" not in st.events["carl"]
    assert st.audit_ok


async def test_a_bad_request_gets_an_answer_instead_of_silence(
    acme: Acme, live_hub: LiveHub
) -> None:
    """The graph cannot parse this; the adapter tells Olga rather than leaving her waiting."""
    st = await story(acme, live_hub, request="Triage something, no numbers")
    assert st.reply.startswith("I could not do that. ValueError: say which claim and amount")
    assert st.task_states == [] and st.approvals == 0  # nothing was sent to Risk


async def test_the_demo_can_be_run_again_on_the_same_org(acme: Acme, live_hub: LiveHub) -> None:
    """Same ports, so the same Agent Card URLs: the second run reuses the registered agents."""
    from setu_sdk.a2a_serve import free_port

    ports = {"risk_port": free_port(), "compliance_port": free_port()}
    first = await story(acme, live_hub, **ports)
    second = await story(acme, live_hub, request="Triage claim #8, amount 600", **ports)
    assert first.seeded.risk["id"] == second.seeded.risk["id"]  # the same registered agent
    assert first.seeded.room_id != second.seeded.room_id and first.seeded.tag != second.seeded.tag
    assert second.reply.startswith("Claim #8: risk LOW (4); cleared by compliance")
    assert second.task_states == ["completed", "completed"] and second.audit_ok


async def test_interactive_mode_sets_the_world_up_and_tells_people_how_to_sign_in(
    acme: Acme, live_hub: LiveHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--interactive`: nothing is scripted. The agents run, people can sign in, and the output
    says who is who and what to type."""
    import httpx

    stop = asyncio.Event()
    seen: list[Any] = []

    def ready(s: Any) -> None:
        seen.append(s)
        stop.set()

    await asyncio.wait_for(
        run_demo.interactive(
            live_hub.url,
            f"admin-{acme.tag}@acme.test",
            PASSWORD,
            web_url="http://localhost:5173",
            ready=ready,
            stop=stop,
        ),
        timeout=60,
    )
    s = seen[0]
    out = capsys.readouterr().out
    assert f"http://localhost:5173/#/rooms/{s.room_id}" in out
    assert s.password in out and s.emails["carl"] in out and "approves the risk step" in out
    assert f"@triage-{s.tag} Triage claim #123" in out
    async with httpx.AsyncClient(base_url=live_hub.url) as http:  # they can sign in as printed
        r = await http.post(
            "/v1/auth/login", json={"email": s.emails["olga"], "password": s.password}
        )
    assert r.status_code == 200
