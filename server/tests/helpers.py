"""Test world builder that goes through the real API (and the real bootstrap command)."""

import asyncio
import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.cli import bootstrap_org
from app.db.session import make_engine, make_sessionmaker

PASSWORD = "correct-horse-battery"


class Person:
    def __init__(self, data: dict[str, Any], token: str) -> None:
        self.id: str = data["id"]
        self.data = data
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}


class Acme:
    """One org with Operations (ops), Risk and Compliance departments and an org admin."""

    def __init__(self, client: TestClient, database_url: str) -> None:
        self.c = client
        self.tag = uuid.uuid4().hex[:6]
        asyncio.run(self._bootstrap(database_url))
        self.admin = self.login(f"admin-{self.tag}@acme.test")
        depts = {d["slug"]: d for d in self.get("/v1/departments", self.admin).json()}
        self.ops = depts["ops"]
        self.risk = self.mk_dept("Risk", "risk")
        self.compliance = self.mk_dept("Compliance", "compliance")

    async def _bootstrap(self, url: str) -> None:
        engine = make_engine(url)
        try:
            async with make_sessionmaker(engine)() as session:
                await bootstrap_org(
                    session,
                    org_name=f"acme-{self.tag}",
                    dept_name="Operations",
                    dept_slug="ops",
                    email=f"admin-{self.tag}@acme.test",
                    display_name="Admin",
                    password=PASSWORD,
                    handle=f"admin-{self.tag}",
                )
        finally:
            await engine.dispose()

    def get(self, path: str, who: Person | None = None, **kw: Any) -> Any:
        return self.c.get(path, headers=who.headers if who else None, **kw)

    def post(self, path: str, who: Person | None, body: dict[str, Any]) -> Any:
        return self.c.post(path, headers=who.headers if who else None, json=body)

    def ok(self, resp: Any, status: int = 201) -> dict[str, Any]:
        assert resp.status_code == status, resp.text
        out: dict[str, Any] = resp.json()
        return out

    def login(self, email: str, password: str = PASSWORD) -> Person:
        data = self.ok(
            self.c.post("/v1/auth/login", json={"email": email, "password": password}), 200
        )
        return Person(data["participant"], data["access_token"])

    def mk_dept(self, name: str, slug: str) -> dict[str, Any]:
        return self.ok(self.post("/v1/departments", self.admin, {"name": name, "slug": slug}))

    def allow_talk(self, a: dict[str, Any] | str, b: dict[str, Any] | str) -> dict[str, Any]:
        """A policy rule letting department `a` address department `b` (cross-department is
        denied by default). Departments are given as dicts, or `*`."""
        ids = [x if isinstance(x, str) else x["id"] for x in (a, b)]
        return self.ok(
            self.post(
                "/v1/policies",
                self.admin,
                {
                    "name": "allow",
                    "rule": {
                        "type": "allow_talk",
                        "from_department": ids[0],
                        "to_department": ids[1],
                    },
                },
            )
        )

    def mk_human(self, dept: dict[str, Any], handle: str, roles: list[str] | None = None) -> Person:
        email = f"{handle}-{self.tag}@acme.test"
        self.ok(
            self.post(
                "/v1/humans",
                self.admin,
                {
                    "department_id": dept["id"],
                    "display_name": handle,
                    "handle": f"{handle}-{self.tag}",
                    "email": email,
                    "password": PASSWORD,
                    "roles": roles or ["dept_member"],
                },
            )
        )
        return self.login(email)

    def mk_agent(self, dept: dict[str, Any], handle: str) -> dict[str, Any]:
        return self.ok(
            self.post(
                "/v1/participants",
                self.admin,
                {
                    "department_id": dept["id"],
                    "display_name": handle,
                    "handle": f"{handle}-{self.tag}",
                },
            )
        )

    def mk_key(self, agent: dict[str, Any], scopes: list[str] | None = None) -> dict[str, Any]:
        body = {"scopes": scopes} if scopes else {}
        return self.ok(self.post(f"/v1/participants/{agent['id']}/api-keys", self.admin, body))

    def mk_room(
        self, who: Person, name: str = "room", visibility: str = "private", **extra: Any
    ) -> dict[str, Any]:
        return self.ok(
            self.post("/v1/rooms", who, {"name": name, "visibility": visibility, **extra})
        )

    def add_member(self, room: dict[str, Any], who: Person, pid: str, role: str = "member") -> None:
        self.ok(
            self.post(f"/v1/rooms/{room['id']}/members", who, {"participant_id": pid, "role": role})
        )

    def say(self, room: dict[str, Any], who: Person, text: str) -> Any:
        return self.post(f"/v1/rooms/{room['id']}/messages", who, {"body": {"text": text}})
