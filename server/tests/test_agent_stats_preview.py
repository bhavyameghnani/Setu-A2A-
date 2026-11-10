"""Agent wizard support: "Test connection" (preview) saves nothing, and per-agent activity stats
are shown only to those who run the agent or oversee the org."""

from tests.a2a_agents import build_v1_agent
from tests.test_a2a_flow import RISK, SKILLS, Scene, hub, register


def test_preview_checks_a_card_without_registering_it(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk Scorer", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        admin = w.mk_human(w.risk, "rita", ["dept_admin"])
        member = w.mk_human(w.risk, "mia")
        url = RISK + "/.well-known/agent-card.json"

        r = w.post("/v1/agents/preview", admin, {"card_url": url})
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["name"] == "Risk Scorer" and got["a2a_version"] == "1.0"
        assert got["suggested_handle"] == "risk-scorer" and got["streaming"] is True
        assert [s["id"] for s in got["skills"]] == ["risk.score"]
        assert got["rpc_url"] == RISK + "/rpc"
        # Nothing was saved: the same card still registers cleanly afterwards.
        assert w.get("/v1/agents", admin).json() == []
        register(w, admin, url, w.risk, handle=f"risk-{w.tag}")

        # Same guards as registering: members cannot, and a dead card says why.
        assert w.post("/v1/agents/preview", member, {"card_url": url}).status_code == 403
        assert w.post("/v1/agents/preview", None, {"card_url": url}).status_code == 401
        dead = w.post("/v1/agents/preview", admin, {"card_url": "http://nowhere.test/card"})
        assert dead.status_code == 502


def test_stats_count_activity_and_respect_who_may_see_them(migrated_database_url: str) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        s.send_task("score claim 7")
        s.task_when("completed")

        for who in (w.admin, s.risk_admin):  # org admin, and the owning department's admin
            rows = w.get("/v1/agents/stats", who).json()
            mine = [r for r in rows if r["participant_id"] == s.pid]
            assert len(mine) == 1, rows
            assert mine[0]["tasks"] == 1 and mine[0]["failed"] == 0
            assert mine[0]["rooms"] == 1 and mine[0]["messages"] >= 2  # updates + result
        # Olga is an ordinary member of another department: no counts for her.
        assert w.get("/v1/agents/stats", s.olga).json() == []
