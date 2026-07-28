"""Shared fixtures: isolated temp SQLite DB + TestClient + fake user folder.

The database path env var must be set BEFORE any ``app.*`` import because
the engine binds at import time — hence the module-level bootstrap here.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vidura_test_"))
os.environ.setdefault("VIDURA_DATABASE_PATH", str(_TMP / "test_app.db"))
os.environ.setdefault("VIDURA_VAR_DIR", str(_TMP / "var"))

import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, engine
from app.main import app


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh tables for every test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def user_folder(tmp_path: Path) -> Path:
    """A realistic user root folder: .sam password, wellness profile,
    trade_history dir. (No real credentials — Kalshi tests are separate.)"""
    (tmp_path / ".sam").write_text("test-pass-123\n", encoding="utf-8")
    (tmp_path / "wellness-profile.json").write_text(
        '{"gender": "Male", "age": "30-35", "ethnicity": "Indian", '
        '"diet": "Non-Vegetarian", "style": "Both", "goals": ["Cholesterol"], '
        '"region": "Bangalore", "notifications": true}',
        encoding="utf-8",
    )
    (tmp_path / "trade_history").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


@pytest.fixture
def user(client: TestClient, user_folder: Path) -> dict:
    resp = client.post(
        "/api/v1/users",
        json={
            "username": "testuser",
            "email": "test@example.com",
            "user_root_folder": str(user_folder),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
