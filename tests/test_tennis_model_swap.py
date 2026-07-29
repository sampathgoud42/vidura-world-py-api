"""Every selectable tennis model must survive the main bot's call signature.

The bug this guards: the main bot calls predict_buy(..., neutral_favorite=...)
(the 07/12 finals rule). v3/v4/v5 grew that parameter; the FROZEN v1/v2
snapshots never did. Selecting v1 therefore raised TypeError on every poll,
which bot_kalshi_sports_v1 swallows into {"action": "WAIT"} — and WAIT is
never logged, so the bot ran a full live session unable to buy, with an empty
error log. sport_adapters/tennis.py now filters kwargs the target cannot take.

Offline: these import the predictor modules directly, no network, no bot.
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import re
import sys
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "runtime" / "prediction-trade"
TENNIS_DIR = RUNTIME / "sports" / "tennis"
ADAPTER = RUNTIME / "kalshi" / "sports" / "sport_adapters" / "tennis.py"
BOT = RUNTIME / "kalshi" / "sports" / "bot_kalshi_sports_v1.py"

MODELS = ["v1", "v2", "v3", "v4", "v5"]

# what the UI offers === what must work
SELECTABLE = MODELS


def _load(model: str):
    path = TENNIS_DIR / f"predict_{model}.py"
    if not path.exists():
        pytest.skip(f"{path.name} not vendored")
    sys.path.insert(0, str(TENNIS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"_t_{model}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(TENNIS_DIR))


def _compat(fn):
    """Mirror of the adapter's wrapper (kept in sync by the test below)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn
    allowed = set(params)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **{k: v for k, v in kwargs.items() if k in allowed})

    return wrapper


MATCH = {"players": [{"name": "A Alpha", "sets_won": 1}, {"name": "B Beta", "sets_won": 1}]}
LIVE = {"A Alpha": 55, "B Beta": 45}
ORIG = {"A Alpha": 60, "B Beta": 40}


def _caller_kwargs() -> set[str]:
    """The kwargs the main bot actually passes, read from its call site — so
    this test keeps up if a new rule adds another parameter."""
    src = BOT.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"predict_buy\(\s*sc,\s*live,\s*orig,(.*?)\)", src, re.S)
    assert m, "could not find the predict_buy call site in bot_kalshi_sports_v1"
    return set(re.findall(r"(\w+)\s*=", m.group(1)))


def test_caller_passes_neutral_favorite():
    """Guards the premise: if the bot stops passing it, this whole class of
    breakage is gone and the wrapper can be reconsidered."""
    assert "neutral_favorite" in _caller_kwargs()


def _live_kwargs() -> dict:
    """Exactly what the bot passes, with plausible values."""
    values = {"ticker": "KXATPMATCH-TEST", "combos": None, "neutral_favorite": False}
    return {k: values.get(k, False) for k in _caller_kwargs()}


@pytest.mark.parametrize("model", SELECTABLE)
def test_every_selectable_model_returns_a_verdict(model):
    mod = _load(model)
    out = _compat(mod.predict_buy)(MATCH, LIVE, ORIG, **_live_kwargs())
    assert isinstance(out, dict)
    assert out.get("action") in {"BUY", "SKIP", "WAIT"}, out


@pytest.mark.parametrize("model", SELECTABLE)
def test_unwrapped_call_is_the_thing_that_used_to_break(model):
    """Documents the failure mode. Models that accept the kwarg pass straight
    through; the frozen ones raise — which is exactly why the wrapper exists."""
    mod = _load(model)
    accepts = "neutral_favorite" in inspect.signature(mod.predict_buy).parameters
    if accepts:
        out = mod.predict_buy(MATCH, LIVE, ORIG, ticker="T", neutral_favorite=False)
        assert out.get("action") in {"BUY", "SKIP", "WAIT"}
    else:
        with pytest.raises(TypeError):
            mod.predict_buy(MATCH, LIVE, ORIG, ticker="T", neutral_favorite=False)


def test_adapter_wraps_every_rebound_callable():
    """The rebind loop must go through _compat, or v1/v2 silently break again."""
    src = ADAPTER.read_text(encoding="utf-8", errors="replace")
    assert "def _compat(" in src, "the kwargs-filtering wrapper is gone"
    assert re.search(r"setattr\(tv1, _n, _compat\(", src), (
        "the model-swap rebind no longer wraps callables through _compat"
    )


def test_compat_is_a_passthrough_for_models_that_accept_everything():
    def fn(a, *, b=None, c=None):
        return {"a": a, "b": b, "c": c}

    assert _compat(fn)(1, b=2, c=3) == {"a": 1, "b": 2, "c": 3}


def test_compat_drops_only_unknown_kwargs():
    def fn(a, *, b=None):
        return {"a": a, "b": b}

    assert _compat(fn)(1, b=2, zzz=9) == {"a": 1, "b": 2}


def test_compat_leaves_varkw_functions_alone():
    def fn(a, **kw):
        return {"a": a, **kw}

    assert _compat(fn)(1, anything=2) == {"a": 1, "anything": 2}
