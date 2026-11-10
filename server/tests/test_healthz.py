from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_ok_with_database(migrated_database_url: str) -> None:
    with TestClient(create_app(migrated_database_url)) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "up"}


def test_healthz_degraded_when_database_down() -> None:
    with TestClient(create_app("postgresql+asyncpg://localhost:1/nope")) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["database"] == "down"
