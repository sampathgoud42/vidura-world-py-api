"""Bot subprocess lifecycle: start, stop, status, logs.

Safety invariants enforced here (learned from real incidents in the source
repo — a 2026-07-02 double-launch corrupted a live ledger, and the legacy
bots default to powering the machine off on halt):

1. One running instance per (user, bot_key) — start returns 409 otherwise.
2. ``HALT_MACHINE_SHUTDOWN=FALSE`` is always forced.
3. While ``settings.paper_only`` is True (the default), every known
   paper/dry-run flag is forced on: DRY_RUN_MODE, BOT152_DRY_RUN,
   MAIN_PAPER; PERP_BUY stays off.
4. The subprocess env NEVER inherits KALSHI_* secrets from the API process;
   each bot family loads its own credentials from the user's folder.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import paths
from app.core.config import get_settings
from app.models import BotRun, User
from app.services import credentials as creds_svc
from app.services.bot_registry import BotSpec, BotVersion, get_bot, script_path

logger = logging.getLogger(__name__)

_LAUNCHER = Path(__file__).with_name("bot_launcher.py")

# Live process handles for runs started by THIS api process (run_id -> Popen).
_PROCESSES: dict[int, subprocess.Popen] = {}

# Serializes the check-then-launch section of start_bot: sync endpoints run
# concurrently in the threadpool, so without this two simultaneous start
# requests could both pass the single-instance check (TOCTOU).
_START_LOCK = threading.Lock()


class BotManagerError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _base_env(mode: str = "paper") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KALSHI")}
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # Never allow a bot halt to power off the host.
            "HALT_MACHINE_SHUTDOWN": "FALSE",
            "PERP_BUY": "FALSE",
        }
    )
    if mode == "live":
        # Only reachable when the server is unlocked (checked in start_bot).
        # The legacy bots default to paper, so live must be explicit.
        env.update(
            {
                "DRY_RUN_MODE": "FALSE",
                "BOT152_DRY_RUN": "FALSE",
                "MAIN_PAPER": "FALSE",
            }
        )
    else:
        env.update(
            {
                "DRY_RUN_MODE": "TRUE",   # btc15 v2-v4, btc60, sports secrets
                "BOT152_DRY_RUN": "TRUE",  # btc15 v5
                "MAIN_PAPER": "TRUE",      # sports main bot
            }
        )
    return env


KNOWN_SPORTS = ("tennis", "baseball")


def _sports_env(env: dict[str, str], options: "BotStartOptions") -> None:
    """Map per-sport options onto the main sports bot's env knobs."""
    if options.sports:
        bad = [s for s in options.sports if s not in KNOWN_SPORTS]
        if bad:
            raise BotManagerError(
                f"Unknown sport(s) {bad}; full-model sports are {list(KNOWN_SPORTS)}", 422
            )
        joined = ",".join(options.sports)
        env["MAIN_SPORTS_LIST"] = joined
        env["SPORTS_LIST"] = joined  # legacy v1/v2 bots read this name
    if options.contracts:
        # blanket size for every selected sport (sport_settings still wins)
        env["SPORT_CONTRACTS"] = str(options.contracts)
        for sport in (options.sports or KNOWN_SPORTS):
            env[f"{sport.upper()}_CONTRACTS"] = str(options.contracts)
    for sport, cfg in (options.sport_settings or {}).items():
        if sport not in KNOWN_SPORTS:
            raise BotManagerError(
                f"Unknown sport '{sport}' in sport_settings; use {list(KNOWN_SPORTS)}", 422
            )
        prefix = sport.upper()
        if cfg.contracts is not None:
            env[f"{prefix}_CONTRACTS"] = str(cfg.contracts)
        if cfg.bank is not None:
            env[f"{prefix}_BANK"] = f"{cfg.bank:g}"
    if options.target_pct is not None:
        env["TARGET_PORTFOLIO_PCT"] = f"{options.target_pct:g}"


class BotStartOptions:
    """Normalized start options passed through from the request schema."""

    def __init__(
        self, mode: str = "paper", sports=None, sport_settings=None, target_pct=None, contracts=None
    ):
        self.mode = mode
        self.sports = sports
        self.sport_settings = sport_settings
        self.target_pct = target_pct
        self.contracts = contracts


def _launch_plan(
    spec: BotSpec, version: BotVersion, user: User, options: BotStartOptions
) -> tuple[list[str], Path, dict[str, str]]:
    """Build (argv, cwd, env) for a bot start honoring each family's contract."""
    settings = get_settings()
    script = script_path(spec, version)
    if not script.is_file():
        raise BotManagerError(f"Bot script missing on disk: {script}", 503)

    user_root = paths.normalize_root(user.user_root_folder)
    if not user_root.is_dir():
        raise BotManagerError(f"User root folder does not exist: {user_root}", 409)

    python = str(settings.bot_python or Path(sys.executable))
    argv = [python, str(_LAUNCHER), str(script)]
    env = _base_env(options.mode)

    trade_dir = user_root / "trade_history"
    log_dir = user_root / "logs"
    trade_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    if options.mode in ("paper", "mock"):
        # Paper runs must not be blocked by the REAL account balance: the
        # btc15 family halts when the live portfolio is under this floor
        # ($100 default), which stops paper sessions on a small account.
        env.setdefault("DO_NOT_BUY_IF_PORTFOLIO_BELOW", "0")

    if spec.launch_style == "cwd_customer":
        # btc15 family: CWD supplies .env + PEM.  Redirect its outputs into
        # the user's folder so multi-user runs never collide on shared files.
        cwd = user_root
        env.setdefault("BOT_CSV_PATH", str(trade_dir / f"{version.version}_trade_history.csv"))
        env.setdefault("BOT_LOG_DIR", str(log_dir))
        env.setdefault("BOT152_CSV_PATH", str(trade_dir / "bot_btc_15_2_trades.csv"))
        if options.contracts:
            # fixed size instead of the %-of-portfolio sizing
            env["KALSHI_CONTRACTS"] = str(options.contracts)
            env["CONTRACTS_PV_PCT"] = "0"
            env["BOT152_CONTRACTS"] = str(options.contracts)
    elif spec.launch_style == "env_customer":
        # btc60 family: resolves paths from __file__, customer via env.
        cwd = script.parent
        env["BTC_CUSTOMERS_DIR"] = str(user_root.parent)
        env["BTC_CUSTOMER"] = user_root.name
        if options.contracts:
            env["KALSHI_CONTRACTS"] = str(options.contracts)
    elif spec.launch_style == "argv_customer":
        # sports family: customer name as argv[1] + SPORTS_* env pins.
        cwd = script.parent
        argv.append(user_root.name)
        env["SPORTS_CUSTOMERS_DIR"] = str(user_root.parent)
        env["SPORTS_CUSTOMER"] = user_root.name
        env_file = user_root / ".env"
        if env_file.is_file():
            env["KALSHI_SPORTS_SECRETS"] = str(env_file)
        env.setdefault("SPORT_LOG_DIR", str(log_dir))
        # Pin per-bot CSV outputs into the user's folder (matched by ingest).
        env.setdefault("MAIN_TRADE_CSV", str(trade_dir / "trade_history_main.csv"))
        env.setdefault("MAIN_PAPER_CSV", str(trade_dir / "paper_trades_main.csv"))
        env.setdefault("BASEBALL_TRADE_CSV", str(trade_dir / "trade_history_baseball.csv"))
        _sports_env(env, options)
    else:  # pragma: no cover - registry misconfiguration
        raise BotManagerError(f"Unknown launch style {spec.launch_style}", 500)
    return argv, cwd, env


def _pid_alive(run: BotRun) -> bool:
    proc = _run_process(run)
    if proc is None:
        return False
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        # Inconclusive — report alive rather than allow a double start.
        return True


def reconcile_runs(db: Session, *, bot_key: str | None = None, user_id: str | None = None) -> list[BotRun]:
    """Sync DB rows with real process state; returns matching runs, newest first."""
    stmt = select(BotRun).order_by(BotRun.started_at.desc())
    if bot_key:
        stmt = stmt.where(BotRun.bot_key == bot_key)
    if user_id:
        stmt = stmt.where(BotRun.user_id == user_id)
    runs = list(db.scalars(stmt).all())
    dirty = False
    for run in runs:
        if run.status != "running":
            continue
        proc = _PROCESSES.get(run.id)
        if proc is not None:
            code = proc.poll()
            if code is not None:
                run.status = "exited" if code == 0 else "failed"
                run.exit_code = code
                run.stopped_at = _utcnow()
                _PROCESSES.pop(run.id, None)
                dirty = True
        elif not _pid_alive(run):
            run.status = "exited"
            run.stopped_at = _utcnow()
            dirty = True
    if dirty:
        db.commit()
    return runs


def running_run(db: Session, bot_key: str, user_id: str) -> BotRun | None:
    runs = reconcile_runs(db, bot_key=bot_key, user_id=user_id)
    for run in runs:
        if run.status == "running":
            return run
    return None


def _check_paper_conflict(user: User) -> None:
    """The bots load the customer .env with override=True, so a stray
    DRY_RUN_MODE=FALSE in the user's folder would silently re-enable live
    trading underneath a requested PAPER run.  Refuse to start instead."""
    try:
        creds = creds_svc.load_kalshi_credentials(user.user_root_folder)
    except creds_svc.CredentialsError:
        return  # no .env at all -> nothing can override our paper flags
    from dotenv import dotenv_values

    values = {k: (v or "") for k, v in dotenv_values(creds.env_file).items()}
    for key in ("DRY_RUN_MODE", "BOT152_DRY_RUN", "MAIN_PAPER"):
        raw = values.get(key, "").strip().upper()
        if raw in ("FALSE", "0", "NO"):
            raise BotManagerError(
                f"{creds.env_file.name} sets {key}={raw}, which would override "
                "this server's paper-only mode inside the bot process. Remove "
                "the line or set it to TRUE before starting.",
                409,
            )


def start_bot(
    db: Session,
    user: User,
    bot_key: str,
    *,
    version: str | None = None,
    mode: str = "paper",
    options: BotStartOptions | None = None,
) -> BotRun:
    spec = get_bot(bot_key)
    ver = spec.version_or_default(version)
    options = options or BotStartOptions(mode=mode)
    options.mode = mode

    if mode == "live":
        if get_settings().paper_only:
            raise BotManagerError(
                "Live trading is locked: this server runs with VIDURA_PAPER_ONLY=true. "
                "Set VIDURA_PAPER_ONLY=false and restart the API to enable live mode.",
                403,
            )
    else:
        # Paper runs must stay paper even if the customer .env says otherwise.
        _check_paper_conflict(user)

    with _START_LOCK:
        existing = running_run(db, bot_key, user.user_id)
        if existing is not None:
            raise BotManagerError(
                f"Bot {bot_key} already running for user {user.username} (run {existing.id})", 409
            )

        argv, cwd, env = _launch_plan(spec, ver, user, options)

        settings = get_settings()
        ts = _utcnow().strftime("%Y%m%d_%H%M%S")
        safe_user = re.sub(r"[^A-Za-z0-9_-]", "_", user.username)[:64]
        log_file = settings.log_dir / f"{bot_key}_{ver.version}_{safe_user}_{ts}.log"

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        log_handle = open(log_file, "a", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise BotManagerError(f"Failed to launch bot process: {exc}", 500) from exc
        finally:
            log_handle.close()  # child holds its own handle

        # Fail fast on immediate crashes (bad .env, lock conflict, import
        # error) instead of reporting a phantom "running" row.
        time.sleep(1.0)
        code = proc.poll()
        if code is not None and code != 0:
            tail = ""
            try:
                tail = "\n".join(
                    log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
                )
            except OSError:
                pass
            raise BotManagerError(
                f"Bot process exited immediately with code {code}. Last log lines:\n{tail}", 502
            )

        extra: dict = {"argv": argv[2:], "cwd": str(cwd)}
        if options.sports:
            extra["sports"] = options.sports
        if options.sport_settings:
            extra["sport_settings"] = {
                k: {"contracts": v.contracts, "bank": v.bank}
                for k, v in options.sport_settings.items()
            }
        if options.target_pct is not None:
            extra["target_pct"] = options.target_pct
        run = BotRun(
            user_id=user.user_id,
            bot_key=bot_key,
            bot_version=ver.version,
            script_path=str(script_path(spec, ver)),
            pid=proc.pid,
            mode=mode,
            status="running",
            log_file=str(log_file),
            extra=extra,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        _PROCESSES[run.id] = proc
    logger.info("Started %s %s for %s (pid %s)", bot_key, ver.version, user.username, proc.pid)
    return run


def stop_bot(db: Session, bot_key: str, *, user_id: str | None = None, run_id: int | None = None) -> list[BotRun]:
    """Stop running processes for the bot: graceful break first (so bot
    finally-blocks can cancel resting orders and release lock files), then a
    hard terminate of the whole tree."""
    runs = reconcile_runs(db, bot_key=bot_key, user_id=user_id)
    targets = [r for r in runs if r.status == "running" and (run_id is None or r.id == run_id)]
    if not targets:
        raise BotManagerError(f"No running {bot_key} bot to stop", 404)
    for run in targets:
        _terminate_tree(run)
        run.status = "stopped"
        run.stopped_at = _utcnow()
        extra = dict(run.extra or {})
        watchdogs = _find_watchdogs(bot_key)
        if watchdogs:
            # The btc60 bots have an optional detached PowerShell watchdog
            # that relaunches them ~30s after a kill. Never kill a process we
            # did not start — surface it instead.
            extra["warning"] = (
                f"External watchdog process(es) alive (pids {watchdogs}); they may "
                "relaunch this bot outside API control. Stop the watchdog manually."
            )
        run.extra = extra
        _PROCESSES.pop(run.id, None)
    db.commit()
    return targets


def _find_watchdogs(bot_key: str) -> list[int]:
    if bot_key != "btc60":
        return []
    # The watchdog is a PowerShell script, so this walk cannot use the
    # python-only fast path; names are cheap, cmdline is read selectively.
    pids = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not (name.startswith("powershell") or name.startswith("pwsh")):
                continue
            if "watchdog_btc60" in " ".join(proc.cmdline()):
                pids.append(proc.info["pid"])
        except psutil.Error:
            continue
    return pids


def _run_process(run: BotRun) -> psutil.Process | None:
    """Resolve the run's psutil process, guarding against PID reuse."""
    if run.pid is None:
        return None
    try:
        proc = psutil.Process(run.pid)
    except psutil.Error:
        return None
    try:
        started = run.started_at.replace(tzinfo=timezone.utc)
        if proc.create_time() > started.timestamp() + 60:
            return None  # PID was recycled by an unrelated process
    except psutil.Error:
        pass  # inconclusive: treat as ours (fail-closed against double start)
    return proc


def _terminate_tree(run: BotRun) -> None:
    parent = _run_process(run)
    if parent is None:
        return
    procs = [parent]
    try:
        procs += parent.children(recursive=True)
    except psutil.Error:
        pass
    # Graceful phase: CTRL_BREAK to the process group lets asyncio bots run
    # their finally blocks (cancel orders, release PID locks).
    if os.name == "nt":
        try:
            os.kill(parent.pid, signal.CTRL_BREAK_EVENT)
            parent.wait(timeout=8)
        except (OSError, psutil.Error):
            pass
    survivors = [p for p in procs if p.is_running()]
    for p in survivors:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(survivors, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass


def tail_log(run: BotRun, lines: int = 100) -> list[str]:
    if not run.log_file:
        return []
    path = Path(run.log_file)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return content[-lines:]
