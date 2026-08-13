from __future__ import annotations


def test_list_tennis_models(client):
    resp = client.get("/api/v1/models/tennis")
    assert resp.status_code == 200
    models = {m["model_id"]: m for m in resp.json()}
    assert set(models) == {"v1", "v2", "v3", "v4", "v5", "v6"}
    v5 = models["v5"]
    assert "whitelist" in v5["name"].lower() or "forensics" in v5["name"].lower()
    assert v5["metrics"]["holdout_roi_pct"] == 30
    assert "KXATPMATCH" in v5["target_markets"]
    # v6 is v5's successor for tennis: the v1 engine under a favourite-only
    # buy policy, listed first so the desk offers it above the older models.
    assert resp.json()[0]["model_id"] == "v6"
    assert "favourite" in models["v6"]["name"].lower()


def test_predictions_roundtrip(client):
    body = {
        "match_name": "Alcaraz vs Sinner",
        "market_ticker": "KXATPMATCH-26JUL27ALCSIN",
        "pick": "Alcaraz",
        "probability": 0.57,
        "confidence": "high",
        "raw": {"situation": "F3", "v5_rule": "F1"},
    }
    resp = client.post("/api/v1/models/tennis/v5/predictions", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["pick"] == "Alcaraz"

    resp = client.get("/api/v1/models/tennis/v5/predictions")
    assert resp.status_code == 200
    preds = resp.json()
    assert len(preds) == 1
    assert preds[0]["probability"] == 0.57


def test_unknown_model_404(client):
    assert client.get("/api/v1/models/tennis/v99/predictions").status_code == 404
    assert (
        client.post(
            "/api/v1/models/tennis/v99/predictions", json={"match_name": "x"}
        ).status_code
        == 404
    )
