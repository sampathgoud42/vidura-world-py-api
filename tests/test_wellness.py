from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def test_profile_imported_from_user_folder(client, user):
    """First GET imports wellness-profile.json from the folder into SQLite."""
    uid = user["user_id"]
    resp = client.get(f"/api/v1/users/{uid}/wellness/profile")
    assert resp.status_code == 200, resp.text
    profile = resp.json()
    assert profile["gender"] == "Male"
    assert profile["age"] == "30-35"
    assert profile["goals"] == ["Cholesterol"]
    assert profile["region"] == "Bangalore"


def test_profile_update_roundtrip_and_file_sync(client, user, user_folder):
    uid = user["user_id"]
    new_profile = {
        "gender": "Male",
        "age": "30-35",
        "ethnicity": "Indian",
        "diet": "Vegetarian",
        "style": "Modern",
        "goals": ["Cholesterol", "Sleep"],
        "region": "Bangalore",
        "notifications": False,
    }
    resp = client.put(f"/api/v1/users/{uid}/wellness/profile", json=new_profile)
    assert resp.status_code == 200
    assert resp.json()["diet"] == "Vegetarian"

    # DB mirrors back out to the folder JSON (source of truth for old apps).
    on_disk = json.loads((user_folder / "wellness-profile.json").read_text())
    assert on_disk["diet"] == "Vegetarian"
    assert on_disk["notifications"] is False


def test_profile_404_when_no_file_or_db(client, tmp_path):
    (tmp_path / "empty").mkdir()
    resp = client.post(
        "/api/v1/users",
        json={"username": "noprofile", "user_root_folder": str(tmp_path / "empty")},
    )
    uid = resp.json()["user_id"]
    assert client.get(f"/api/v1/users/{uid}/wellness/profile").status_code == 404


def test_wellness_data_60_day_window(client, user):
    uid = user["user_id"]
    now = datetime.now(timezone.utc)

    def add(kind: str, days_ago: int):
        resp = client.post(
            f"/api/v1/users/{uid}/wellness/data",
            json={
                "kind": kind,
                "payload": {"note": f"{days_ago}d ago"},
                "recorded_at": (now - timedelta(days=days_ago)).isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text

    add("checkin", 1)
    add("checkin", 30)
    add("checkin", 59)
    add("checkin", 90)  # outside the 60-day default window

    resp = client.get(f"/api/v1/users/{uid}/wellness/data")
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 3  # 90d entry excluded by default

    resp = client.get(f"/api/v1/users/{uid}/wellness/data", params={"days": 366})
    assert len(resp.json()) == 4

    resp = client.get(f"/api/v1/users/{uid}/wellness/data", params={"kind": "nope"})
    assert resp.json() == []


def test_profile_update_recorded_as_entry(client, user):
    uid = user["user_id"]
    client.put(
        f"/api/v1/users/{uid}/wellness/profile",
        json={"gender": "Male", "goals": ["Fertility"]},
    )
    resp = client.get(
        f"/api/v1/users/{uid}/wellness/data", params={"kind": "profile_update"}
    )
    assert len(resp.json()) == 1


def test_options_endpoint(client, user):
    resp = client.get(f"/api/v1/users/{user['user_id']}/wellness/options")
    assert resp.status_code == 200
    options = resp.json()
    assert "goals" in options and "Cholesterol" in options["goals"]
