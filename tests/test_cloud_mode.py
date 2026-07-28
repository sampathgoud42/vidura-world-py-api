"""Cloud profile: read APIs keep working, execution endpoints 503."""

from __future__ import annotations

import pytest

from app.core.config import get_settings


@pytest.fixture
def cloud(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "cloud_mode", True)
    yield settings


def test_execution_endpoints_are_503_in_cloud(client, user, cloud):
    uid = user["user_id"]
    body = {"user_id": uid, "mode": "paper"}
    for method, path, payload in [
        ("post", "/api/v1/bots/btc/start?bot=btc15", body),
        ("post", "/api/v1/bots/btc/stop?bot=btc15", body),
        ("post", "/api/v1/bots/sports/start", body),
        ("post", "/api/v1/bots/sports/stop", body),
        ("post", "/api/v1/super/on", None),
        ("post", "/api/v1/super/off", {}),
        ("post", "/api/v1/super/regenerate?force=true", None),
        ("post", "/api/v1/super/econ/refresh", None),
    ]:
        resp = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
        assert resp.status_code == 503, f"{path} -> {resp.status_code}"
        assert "cloud mode" in resp.json()["detail"]


def test_read_endpoints_still_work_in_cloud(client, user, super_dir, cloud):
    # seed the DB mirror while the (fixture) repo is present, as a local
    # instance would before shipping the database to the cloud
    assert client.post("/api/v1/super/sync").status_code == 200

    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/users").status_code == 200
    assert client.get("/api/v1/bots").status_code == 200
    assert client.get(f"/api/v1/users/{user['user_id']}/trades").status_code == 200
    assert client.get("/api/v1/models/tennis").status_code == 200

    state = client.get("/api/v1/super/state").json()
    assert "error" not in state and state["categories"]
    assert client.get("/api/v1/super/signals").json()["total"] > 0
    assert client.get("/api/v1/super/gex").status_code == 200
    assert client.get("/api/v1/bots/sports/performance").status_code == 200


def test_database_url_override_and_postgres_normalisation(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url_override", "postgres://u:p@host:5432/db")
    assert settings.database_url == "postgresql+psycopg://u:p@host:5432/db"
    assert settings.is_sqlite is False

    monkeypatch.setattr(settings, "database_url_override", "")
    assert settings.database_url.startswith("sqlite:///")
    assert settings.is_sqlite is True


def test_process_helpers_degrade_without_psutil(monkeypatch):
    """The cloud image ships no psutil — helpers must no-op, not crash."""
    from app.services import bot_manager, super_research

    monkeypatch.setattr(super_research, "psutil", None)
    monkeypatch.setattr(bot_manager, "psutil", None)
    assert list(super_research.iter_python_processes()) == []
    assert bot_manager._find_watchdogs("btc60") == []
