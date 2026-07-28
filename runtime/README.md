# runtime/ — the vendored trading runtime

Everything the API executes lives here. **Nothing is read from
`D:\_projects\38trades-py-claude` any more**; `settings.source_repo`
defaults to this folder (`app/core/config.py`).

```
runtime/
├── prediction-trade/
│   ├── kalshi/
│   │   ├── btc/            btc.env · btc/ (monitor, liquidity_sr, cb_btc_signal)
│   │   │   ├── btc15/      v2 v3 v4 v5 bots
│   │   │   └── btc60/      fable5 · burst
│   │   └── sports/         bot_kalshi_main · v1 · v2 · kalshi_sports · sport_adapters/
│   │                       kaslhi_sports.env  (the misspelling is load-bearing)
│   └── sports/
│       ├── tennis/         predict_v3 · predict_v5 · rankings · combinations CSV
│       └── baseball/       sabermetric model + sports4cast scraper
├── indicators/             imported as `btc` by the sports bots (NOT the same
│                           package as kalshi/btc/btc — both are required)
├── super_research/         super_signal_bot · engine_common · <ticker>_research/
│                           super_research.config · a/b_signals.csv · archive/
│                           gex/ · gex_daily.json · econ_today.json
├── NIFTY_research/         India workers — MUST stay siblings of super_research
└── BANKNIFTY_research/     (their sys.path hack is HERE.parent/"super_research")
```

## Layout rules — these are not cosmetic

The scripts resolve everything from `__file__`, so the directory depths
above are part of the contract:

- `sport_adapters/generic.py` walks `parents[3]/"sports"`;
- the sports bots walk `parents[2]` for the project root and alias
  `sys.modules["btc"] = indicators`;
- the btc15 bots insert `.../kalshi/btc` on `sys.path` to import `btc`;
- `bot_launcher.py` scans ancestors for `btc/btc15` to alias the renamed
  module `bot_kalshi_btc15` → `v4_bot_kalshi_btc15.py`;
- the India workers insert `HERE.parent/"super_research"`.

Flattening or re-nesting any of it breaks imports at bot start.

## Local edits made during vendoring

- `v5_bot_btc_15_2.py` — the two absolute defaults pointing at the old
  checkout's `indicators/` now resolve from `__file__`.
- `indicators/btc15_quarter.bat` — `cd /d <old repo>` → `cd /d "%~dp0"`, and
  the hardcoded interpreter path → `python`.

## Not copied (and why)

`__pycache__/` (a stale `.pyc` for the renamed module would shadow the
launcher's alias), per-ticker `cache/` (bar caches, regenerate on demand),
`results/all_configs.csv` (~80 MB of backtest sweeps; only
`engine_scores.json` is read at runtime), `*.lock` (a copied PID lock makes
a bot refuse to start), logs, research/backtest scripts, and
`top100_research/` (retired).

## Secrets

`super_research/flashalpha.env` holds a live API key and is **gitignored** —
this repo is public. On a fresh clone, recreate it (or set
`VIDURA_FLASHALPHA_API_KEY`). Kalshi credentials are *not* here: they stay
in the per-user folders under `VIDURA_CUSTOMERS_ROOT`.
