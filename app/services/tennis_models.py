"""Tennis prediction model registry.

The models are pure-function libraries in the source repo
(``prediction-trade/sports/tennis/predict*.py``).  Version numbering there is
historical and misleading, so the registry captures ground truth from the
2026-07-16 forensics work rather than filename order.  Metrics quoted are the
documented studies; pre-07/16 CSV P&L is known-phantom and excluded.
"""

from __future__ import annotations

from app.core.config import get_settings

_TENNIS_DIR = "prediction-trade/sports/tennis"

TENNIS_MODELS: list[dict] = [
    {
        "model_id": "v5",
        "version": "v5",
        "name": "Forensics whitelist (production)",
        "description": (
            "Current main-bot model. Ghost-state guard at set boundaries, "
            "hard-coded 50-62c entry band, entry_gate whitelist rules F1-F4, "
            "flat sizing, ultra always downgraded to high. Built 2026-07-16 "
            "from 554 trades with ground-truth P&L from Kalshi fills/settlements."
        ),
        "target_markets": ["KXATPMATCH", "KXWTAMATCH", "KXCHALLENGERMATCH", "KXITFMATCH"],
        "metrics": {
            "train_pnl_usd": 200.91,
            "train_trades": 64,
            "holdout_pnl_usd": 60.63,
            "holdout_trades": 17,
            "holdout_roi_pct": 30,
            "baseline_pnl_usd": -1486.0,
        },
        "script": f"{_TENNIS_DIR}/predict_v5.py",
    },
    {
        "model_id": "v4",
        "version": "v4",
        "name": "Ultra-gate wrapper",
        "description": (
            "Thin wrapper over v3: ultra confidence requires bid >= 70c, "
            "situation E against the favorite half-sized, mid-band 50-69c "
            "favorite-only. Ran the main bot before 2026-07-16."
        ),
        "target_markets": ["KXATPMATCH", "KXWTAMATCH", "KXITFMATCH"],
        "metrics": {"status": "superseded by v5"},
        "script": f"{_TENNIS_DIR}/predict_v4.py",
    },
    {
        "model_id": "v3",
        "version": "v3",
        "name": "Situation engine + exit rules (standalone v1 bot)",
        "description": (
            "v1 snapshot plus three exit rules (favorite comeback / favorite "
            "collapse / bought-high collapse), determine_favorite ladder, "
            "ultra-favorite upgrade, set-1 underdog block. Powers the "
            "standalone bot_kalshi_sports_v1.py."
        ),
        "target_markets": ["KXATPMATCH", "KXWTAMATCH", "KXITFMATCH"],
        "metrics": {"study_2026_06_28": "68-84c band 80% win rate (settlement study)"},
        "script": f"{_TENNIS_DIR}/predict_v3.py",
    },
    {
        "model_id": "v2",
        "version": "v2",
        "name": "Frozen 2026-07-06 snapshot",
        "description": "Snapshot of the evolving engine as it ran on 07/06 (reference only).",
        "target_markets": ["KXATPMATCH", "KXWTAMATCH", "KXITFMATCH"],
        "metrics": {"status": "reference snapshot"},
        "script": f"{_TENNIS_DIR}/predict_v2.py",
    },
    {
        "model_id": "v1",
        "version": "v1",
        "name": "Frozen profitable-weekend snapshot",
        "description": (
            "Frozen 2026-07-06 snapshot of the profitable 07/02-07/04 weekend "
            "state (keeps a known break-point bug; rollback reference only)."
        ),
        "target_markets": ["KXATPMATCH", "KXWTAMATCH", "KXITFMATCH"],
        "metrics": {"status": "reference snapshot"},
        "script": f"{_TENNIS_DIR}/predict_v1.py",
    },
]


def list_models() -> list[dict]:
    repo = get_settings().source_repo
    out = []
    for m in TENNIS_MODELS:
        item = dict(m)
        item["exists"] = (repo / m["script"]).is_file()
        item["script"] = str(repo / m["script"])
        out.append(item)
    return out


def get_model(model_id: str) -> dict | None:
    for m in list_models():
        if m["model_id"] == model_id:
            return m
    return None
