#!/usr/bin/env python3
"""
sport_adapters/tennis.py — tennis plugin for bot_kalshi_main.py.
================================================================
Thin adapter over the PROVEN tennis stack in ``bot_kalshi_sports_v1``
(Kalshi milestone scoring, SofaScore rankings) — delegating rather than
duplicating.  The MODEL is predict_v5 (user 07/16): the forensics-derived
whitelist built from the 554-trade study with true P&L rebuilt from the
Kalshi API — 50-62c price gate, F1-F4 whitelist, ghost-double-break guard,
flat sizing, never ultra.  The standalone v1 bot keeps predict_v3 untouched.

V5 EXECUTION SPEC (this adapter + the engine):
  • ONE ENTRY PER MATCH, EVER — persistent event-ticker ledger in the engine
    (rebuy/averaging-down loops were -$1,126 of the -$1,539 true loss);
  • maker-first entries — price_bump_c=0 (rest at the bid), 60s fill window,
    cancel if unfilled (taker bump+spread+fees cost $958 = 62% of the loss);
  • resting 97c TP ALWAYS (tp_for_entry below — flip insurance kept +$873);
  • entry-relative stop at entry-20c, ONE strike (stop_confirm=1), which the
    spread guard may never defer; 30c hard floor as the gapped-through
    backstop; flat 20 contracts, no confidence upsizing;
  • API-reconciled dollar brakes: day -$60 / week -$150 halt new entries
    (the old internal-P&L bank halt never tripped because $0-settled losers
    were recorded as 0.00).
"""
from __future__ import annotations

import functools
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # kalshi/sports
import bot_kalshi_sports_v1 as tv1                              # noqa: E402

# MODEL SELECTION (vidura-world): TENNIS_MODEL picks which predict_* module
# this process runs. v5 stays the default — it is the forensics-derived
# whitelist and the only model validated on ground-truth P&L. The others are
# selectable for comparison runs:
#   v5  production whitelist (default)   v4  ultra-gate wrapper over v3
#   v3  situation engine + exits         v2  frozen 07/06 snapshot
#   v1  frozen profitable-weekend snapshot
import importlib                                                # noqa: E402

TENNIS_MODEL = (os.getenv("TENNIS_MODEL") or "v5").strip().lower()
if TENNIS_MODEL in ("", "default"):
    TENNIS_MODEL = "v5"
if TENNIS_MODEL not in ("v1", "v2", "v3", "v4", "v5"):
    print(f"  [TENNIS] unknown TENNIS_MODEL={TENNIS_MODEL!r} — falling back to v5",
          flush=True)
    TENNIS_MODEL = "v5"
try:
    p5 = importlib.import_module(f"tennis.predict_{TENNIS_MODEL}")
except ImportError as _exc:                                     # pragma: no cover
    print(f"  [TENNIS] predict_{TENNIS_MODEL} unavailable ({_exc}) — using v5",
          flush=True)
    TENNIS_MODEL = "v5"
    p5 = importlib.import_module("tennis.predict_v5")

# v4 rule retained in v5 (user 07/13): no favorite-based firesell exits for
# tennis — exits are the v5 bands/stop only. TRUE restores the v3 exits.
TENNIS_FIRESELL_EXITS = os.getenv("TENNIS_FIRESELL_EXITS",
                                  "FALSE").strip().upper() == "TRUE"

# MODEL SWITCH (user 07/16): the MAIN bot runs predict_v5 by default (see
# module doc). tv1 imported its model symbols from predict_v3 at module load,
# so rebind them here — this affects ONLY this process; the standalone v1 bot
# keeps v3. Older models do not define every symbol (the exit rules arrived in
# v3, the whitelist in v5), so rebind only what the chosen model actually has
# and leave tv1's v3 defaults in place for the rest.
#
# The models' signatures drifted as rules were added: the caller passes
# neutral_favorite= (the 07/12 finals rule), which v3/v4/v5 accept but the
# FROZEN v1/v2 snapshots do not. Swapping v1 in raw therefore made every poll
# raise TypeError, which the caller swallows into {"action": "WAIT"} — and a
# WAIT is never logged, so v1/v2 sat silent for a whole session without ever
# being able to buy. Filter kwargs the target cannot take instead of editing
# the frozen snapshots. A model that predates a rule simply does not apply it.
def _compat(fn):
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn
    allowed = set(params)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        dropped = [k for k in kwargs if k not in allowed]
        if dropped:
            _warn_dropped(fn, dropped)
        return fn(*args, **{k: v for k, v in kwargs.items() if k in allowed})

    return wrapper


_WARNED: set[str] = set()


def _warn_dropped(fn, dropped) -> None:
    """Say it once per (function, kwarg) so a silent behaviour gap is visible
    in the log without spamming every 30s poll."""
    for k in dropped:
        key = f"{getattr(fn, '__name__', fn)}:{k}"
        if key in _WARNED:
            continue
        _WARNED.add(key)
        print(f"  [TENNIS] {TENNIS_MODEL} predates '{k}' — that rule is not applied",
              file=sys.stderr, flush=True)


for _n in ("predict_buy", "favorite_comeback_exit", "favorite_collapse_exit",
           "bought_high_collapse_exit", "determine_favorite", "_md_from_kalshi",
           "BID_LO", "BID_HI", "CONF_ULTRA", "CONF_HIGH"):
    if hasattr(p5, _n):
        _v = getattr(p5, _n)
        setattr(tv1, _n, _compat(_v) if callable(_v) else _v)

from .base import SportAdapter                                  # noqa: E402


class TennisAdapter(SportAdapter):
    name = "tennis"
    DEFAULTS = {
        # v5 exits: entry-20c one-strike stop + 97c profit lock + 30c backstop
        "stop_loss_c": 20,           # entry-relative stop (TENNIS_STOP_LOSS_C)
        "stop_confirm": 1,           # ONE strike — no "breach 1/2 watching"
        "tp_ceiling_c": 97,          # profit lock (TENNIS_TP_CEILING_C overrides)
        "sl_floor_c": 30,            # gapped-through backstop: never hold <30c
        # v5 execution: maker-first entry, flat-20 sizing, API dollar brakes
        "price_bump_c": 0,           # rest at the bid — never cross +5c
        "fill_timeout_s": 60,        # unfilled after 60s → cancel, forget
        "contracts": 20,
        "day_loss_halt_usd": 60,     # API-reconciled; halt until 09:00 CST next day
        "week_loss_halt_usd": 150,   # API-reconciled; halt for human review
        "max_live_hours": tv1.SPORT_MAX_LIVE_HOURS,
        "poll_s": tv1.SPORT_POLL_S,
    }

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.rankings: dict = {}
        self.rank_tours: dict = {}
        self.combos: dict = {}
        self._who: dict = {}          # ticker -> player name (ESPN score fallback)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _rankings_updated_today(self) -> bool:
        """True if tennis_rankings.csv's last-modified date is today (CST)."""
        try:
            p = tv1._rankings_csv_path()
            cst = ZoneInfo("America/Chicago")
            return (datetime.fromtimestamp(p.stat().st_mtime, cst).date()
                    == datetime.now(cst).date())
        except Exception:
            return False

    async def startup(self, client) -> None:
        # user 07/13: ensure tennis rankings are scraped for TODAY. First launch
        # each day (CSV not from today, or missing) forces a fresh scrape; later
        # launches see today's timestamp and skip (the browser scrape is slow).
        if tv1._HAS_TENNIS_SCORES and tv1.SPORT_SCRAPE_RANKINGS:
            fresh_today = self._rankings_updated_today()
            print(f"  [tennis] rankings {'already scraped today — up to date' if fresh_today else 'not from today — scraping now'}")
            await tv1._scrape_tennis_rankings(force=not fresh_today)
        self.combos = tv1.load_combinations() if tv1._HAS_PREDICT else {}
        self.on_refresh()
        if tv1._HAS_TENNIS_SCORES and not self.rankings:
            print("  [tennis] rankings CSV missing/empty — ranks will show NA",
                  file=sys.stderr)

    def on_refresh(self) -> None:
        if tv1._HAS_TENNIS_SCORES:
            try:
                self.rankings = tv1.load_rankings_csv()
                self.rank_tours = tv1.load_rank_tours()
            except Exception:
                pass

    def describe(self) -> str:
        return (super().describe()
                + f" model={TENNIS_MODEL} (band {getattr(p5, 'BID_LO', '?')}-"
                  f"{getattr(p5, 'BID_HI', '?')}c whitelist, ghost-guard, "
                  f"flat size) entry=maker+{self.cfg.price_bump_c}c/{self.cfg.fill_timeout_s}s "
                  f"exits=[TP {self.cfg.tp_ceiling_c}c, stop entry-{self.cfg.stop_loss_c}c "
                  f"x{self.cfg.stop_confirm}, floor {self.cfg.sl_floor_c}c] "
                  f"brakes=[day ${self.cfg.day_loss_halt_usd:.0f}, "
                  f"week ${self.cfg.week_loss_halt_usd:.0f}]")

    # ── discovery ────────────────────────────────────────────────────────────
    async def confirm_active(self, matches: list, client) -> list:
        """SofaScore live-status confirmation (fails open, exactly like v1)."""
        if not (tv1.SPORT_CONFIRM_ACTIVE and tv1._HAS_PREDICT and matches):
            return matches
        try:
            statuses = await tv1.live_statuses()
        except Exception:
            statuses = []
        if not statuses:
            return matches
        keep = []
        for m in matches:
            st = tv1._status_for_match(m, statuses)
            if st in tv1._DROP_STATUSES:
                print(f"  [status] {m['ticker']} status={st!r} — not active, removed")
                continue
            keep.append(m)
        return keep

    async def match_meta(self, client, ticker) -> tuple:
        return await tv1._match_meta(client, ticker)   # also caches _TOURNAMENT

    def note_match(self, m: dict) -> None:
        self._who[m["ticker"]] = m.get("yes_sub_title") or m.get("title")

    def label(self, ticker: str) -> str:
        return str(tv1._TOURNAMENT.get(tv1._event_of(ticker), ""))

    # ── evaluation ───────────────────────────────────────────────────────────
    async def evaluate(self, client, ticker) -> "dict | None":
        return await tv1._eval_one_tennis(client, ticker, self.rankings, self.combos,
                                          who=self._who.get(ticker),
                                          rank_tours=self.rank_tours)

    def context(self, sig: dict) -> str:
        return f"set {sig.get('set_num')}" if sig.get("set_num") else ""

    # ── execution policy ─────────────────────────────────────────────────────
    async def market_for(self, client, ticker, name: str) -> "tuple | None":
        pm = await tv1._player_markets(client, ticker)
        mkt = pm.get(name)
        return (mkt, "yes") if mkt else None

    async def claim_tickers(self, client, ticker) -> list:
        pm = await tv1._player_markets(client, ticker)
        return [t for t in pm.values() if t] or [ticker]

    def size_contracts(self, sig: dict) -> int:
        # v5 (user 07/16): FLAT sizing — confidence NEVER scales size up (ultra
        # was anti-calibrated: -$5.03/trade vs high -$2.42, and 69% of the
        # loss-cohort dollars sat above flat-20). size_mult may only shrink.
        n = self.cfg.contracts
        try:
            n = round(n * min(1.0, float(sig.get("size_mult", 1) or 1)))
        except (TypeError, ValueError):
            pass
        return max(1, n)

    def tp_for_entry(self, ticker: str, entry_c: int,
                     sig: "dict | None" = None) -> "int | None":
        """v5 TP policy (user 07/16): a resting 97c GTC sell ALWAYS, regardless
        of SPORT_SELL — the forensics showed resting TPs fill on 81% of winners
        (printing +3.9c above target) and their flip-insurance sold 9 eventual
        losers for +$873.  TP+30%% was the worst policy tested (-$1,087 vs
        ride) and is gone; finals get the same 97c, no special band."""
        if not entry_c:
            return None
        return self.cfg.tp_ceiling_c

    # ── sport-specific exits (v3 rules, adapted from the v1 guardian) ────────
    async def exit_check(self, client, ticker, entry_c, held_bid_c, side,
                         position) -> tuple:
        # v4 (user 07/13): tennis firesells removed — only the engine's 97c/7c
        # price bands exit a held tennis position. Restore with TENNIS_FIRESELL_EXITS=TRUE.
        if not TENNIS_FIRESELL_EXITS:
            return False, "", False
        if not tv1._HAS_PREDICT:
            return False, "", False
        if not (tv1.SPORT_FAV_COMEBACK_EXIT or tv1.SPORT_HIGH_COLLAPSE_EXIT):
            return False, "", False
        try:
            sc = await tv1.get_kalshi_tennis_score(client, ticker)
        except Exception:
            return False, "", False
        if not sc:
            return False, "", False
        for pl in sc.get("players", []):
            rk = tv1.rank_for(pl.get("name", ""), self.rankings)
            pl["rank"] = rk if rk is not None else None
            pl["rank_tour"] = tv1.tour_for(pl.get("name", ""), self.rank_tours)
        md = tv1._md_from_kalshi(sc)
        players = sc.get("players") or []
        if md is None or len(players) < 2:
            return False, "", False
        pm = await tv1._player_markets(client, ticker)
        held_name = {mt: nm for nm, mt in pm.items()}.get(ticker)

        async def _bid_c_of(name):
            mkt = pm.get(name)
            if not mkt:
                return None
            try:
                b = await tv1.v1._bid_price(client, mkt, "yes")
            except Exception:
                b = None
            return round(b * 100) if b is not None else None

        do_exit, why = False, ""
        # 1) bought-high collapse (favorite-independent, user 07/10)
        if tv1.SPORT_HIGH_COLLAPSE_EXIT:
            do_exit, why = tv1.bought_high_collapse_exit(entry_c or None,
                                                         held_bid_c, players)
        # 2/3) favorite-comeback / favorite-collapse (skipped for finals)
        if tv1._event_of(ticker) not in tv1._TOURNAMENT:   # finals rule needs the label
            await tv1._match_meta(client, ticker)
        if (not do_exit and tv1.SPORT_FAV_COMEBACK_EXIT
                and not tv1._is_final_round(ticker)):
            try:
                _live, og = await tv1.get_player_odds(client, ticker,
                                                      sc.get("match_start"))
            except Exception:
                og = {}
            md["favorite"] = tv1.determine_favorite(
                (players[0].get("name", ""), players[1].get("name", "")), og,
                (players[0].get("rank"), players[1].get("rank")),
                rank_tours=(players[0].get("rank_tour"), players[1].get("rank_tour")))
            fav = md.get("favorite")
            held_side = (None if held_name is None
                         else "A" if held_name == players[0].get("name")
                         else "B" if held_name == players[1].get("name") else None)
            if fav is not None and held_side is not None:
                fav_name = players[0]["name"] if fav == "A" else players[1]["name"]
                opp_name = players[1]["name"] if fav == "A" else players[0]["name"]
                if held_name == fav_name:
                    do_exit, why = tv1.favorite_collapse_exit(
                        md, held_side, await _bid_c_of(opp_name))
                else:
                    do_exit, why = tv1.favorite_comeback_exit(
                        md, held_side, entry_c or None, held_bid_c,
                        await _bid_c_of(fav_name))
        immediate = "double-break" in (why or "")          # score-confirmed → no debounce
        return do_exit, why, immediate
