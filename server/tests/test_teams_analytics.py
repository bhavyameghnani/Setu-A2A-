"""Teams (named groups, department-owned) and the analytics summary (scoped by department)."""

from typing import Any

from fastapi.testclient import TestClient

from tests.a2a_agents import build_v1_agent
from tests.test_a2a_flow import RISK, SKILLS, Scene, hub


def req(c: TestClient, method: str, path: str, who: Any, body: dict[str, Any] | None = None) -> Any:
    return c.request(method, path, headers=who.headers, json=body)


def test_teams_are_department_owned_groups(migrated_database_url: str) -> None:
    with hub(migrated_database_url, {}) as (w, _, client):
        rita = w.mk_human(w.risk, "rita", ["dept_admin"])  # Risk admin
        oz = w.mk_human(w.ops, "oz", ["dept_admin"])  # Ops admin
        mia = w.mk_human(w.risk, "mia")  # plain member
        bot = w.mk_agent(w.ops, "bot")

        made = w.post(
            "/v1/teams", rita, {"name": "Scoring", "member_ids": [mia.id, bot["id"], mia.id]}
        )
        assert made.status_code == 201, made.text
        team = made.json()
        assert team["department_id"] == w.risk["id"] and team["created_by"] == rita.id
        assert team["member_ids"] == [mia.id, bot["id"]]  # de-duplicated, any department

        # Everyone in the org can read teams; only the owning department's admins change them.
        assert [t["name"] for t in w.get("/v1/teams", mia).json()] == ["Scoring"]
        assert w.post("/v1/teams", mia, {"name": "Nope"}).status_code == 403
        assert req(client, "PATCH", f"/v1/teams/{team['id']}", oz, {"name": "x"}).status_code == 403
        assert req(client, "DELETE", f"/v1/teams/{team['id']}", oz).status_code == 403
        assert w.post("/v1/teams", rita, {"name": "Scoring"}).status_code == 409
        assert (
            w.post("/v1/teams", oz, {"name": "Other", "department_id": w.risk["id"]}).status_code
            == 403
        )
        assert (
            w.post(
                "/v1/teams", rita, {"name": "Ghost", "member_ids": [bot["id"][:-1] + "0"]}
            ).status_code
            == 404
        )

        # Members: add is idempotent, remove works, the org admin can do it too.
        again = w.post(f"/v1/teams/{team['id']}/members", rita, {"participant_id": mia.id})
        assert again.status_code == 200 and again.json()["member_ids"] == [mia.id, bot["id"]]
        gone = req(client, "DELETE", f"/v1/teams/{team['id']}/members/{mia.id}", w.admin)
        assert gone.status_code == 200 and gone.json()["member_ids"] == [bot["id"]]
        renamed = req(client, "PATCH", f"/v1/teams/{team['id']}", rita, {"name": "Risk scoring"})
        assert renamed.json()["name"] == "Risk scoring"

        assert req(client, "DELETE", f"/v1/teams/{team['id']}", rita).status_code == 204
        assert w.get("/v1/teams", rita).json() == []


def test_analytics_summary_counts_tasks_and_scopes_by_department(
    migrated_database_url: str,
) -> None:
    app = build_v1_agent(RISK, name="Risk", skills=SKILLS)
    with hub(migrated_database_url, {"risk.test": app}) as (w, _, _):
        s = Scene(w)
        s.send_task("score claim 1")
        s.task_when("completed")
        carl = w.mk_human(w.compliance, "carl")  # a department that has nothing to do with it

        for who, scope in ((w.admin, "org"), (s.olga, "department"), (s.risk_admin, "department")):
            r = w.get("/v1/analytics/summary?days=7", who)
            assert r.status_code == 200, r.text
            got = r.json()
            assert got["scope"] == scope and got["days"] == 7
            assert got["total"] == 1 and got["completed"] == 1 and got["failed"] == 0
            assert got["success_rate"] == 1.0 and got["active"] == 0
            assert got["avg_seconds"] is not None and got["avg_seconds"] >= 0
            assert len(got["per_day"]) == 7
            assert sum(d["created"] for d in got["per_day"]) == 1
            assert got["per_day"][-1]["completed"] == 1  # today, UTC
            assert [a["participant_id"] for a in got["by_agent"]] == [s.pid]
            assert got["by_agent"][0]["tasks"] == 1 and got["by_agent"][0]["completed"] == 1

        other = w.get("/v1/analytics/summary", carl).json()
        assert other["total"] == 0 and other["success_rate"] is None and other["by_agent"] == []
        assert len(other["per_day"]) == 14  # the default window, zero-filled
        assert w.get("/v1/analytics/summary?days=0", w.admin).status_code == 422
        assert w.get("/v1/analytics/summary", None).status_code == 401
