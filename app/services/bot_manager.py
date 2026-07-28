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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import paths
from app.core.config import get_settings
from app.models import BotRun, User
from app.services.bot_registry import BotSpec, BotVersion, get_bot, script_path

logger = logging.getLogger(__name__)

_LAUNCHER = Path(__file__).with_name("bot_launcher.py")

# Live process handles for runs started by THIS api process (run_id -> Popen).
_PROCESSES: dict[int, subprocess.Popen] = {}


class BotManagerError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _base_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KALSHI")}
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            # Never allow a bot halt to power off the host.
            "HALT_MACHINE_SHUTDOWN": "FALSE",
        }
    )
    if get_settings().paper_only:
        env.update(
            {
                "DRY_RUN_MODE": "TRUE",   # btc15 v2-v4, btc60, sports secrets
                "BOT152_DRY_RUN": "TRUE",  # btc15 v5
                "MAIN_PAPER": "TRUE",      # sports main bot
                "PERP_BUY": "FALSE",
            }
        )
    return env


def _launch_plan(
    spec: BotSpec, version: BotVersion, user: User
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
    env = _base_env()

    trade_dir = user_root / "trade_history"
    log_dir = user_root / "logs"
    trade_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    if spec.launch_style == "cwd_customer":
        # btc15 family: CWD supplies .env + PEM.  Redirect its outputs into
        # the user's folder so multi-user runs never collide on shared files.
        cwd = user_root
        env.setdefault("BOT_CSV_PATH", str(trade_dir / f"{version.version}_trade_history.csv"))
        env.setdefault("BOT_LOG_DIR", str(log_dir))
        env.setdefault("BOT152_CSV_PATH", str(trade_dir / "bot_btc_15_2_trades.csv"))
    elif spec.launch_style == "env_customer":
        # btc60 family: resolves paths from __file__, customer via env.
        cwd = script.parent
        env["BTC_CUSTOMERS_DIR"] = str(user_root.parent)
        env["BTC_CUSTOMER"] = user_root.name
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
    else:  # pragma: no cover - registry misconfiguration
        raise BotManagerError(f"Unknown launch style {spec.launch_style}", 500)
    return argv, cwd, env


def _pid_alive(run: BotRun) -> bool:
    if run.pid is None:
        return False
    try:
        proc = psutil.Process(run.pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        # Guard against PID reuse: the process must be at least as old as
        # the recorded start (with 60s of clock slack). started_at is naive UTC.
        started = run.started_at.replace(tzinfo=timezone.utc)
        return proc.create_time() <= started.timestamp() + 60
    except psutil.NoSuchProcess:
        return False
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


def start_bot(
    db: Session,
    user: User,
    bot_key: str,
    *,
    version: str | None = None,
    mode: str = "paper",
) -> BotRun:
    spec = get_bot(bot_key)
    ver = spec.version_or_default(version)

    if get_settings().paper_only and mode not in ("paper", "mock"):
        raise BotManagerError("This server is configured paper-only; mode must be 'paper' or 'mock'", 403)

    existing = running_run(db, bot_key, user.user_id)
    if existing is not None:
        raise BotManagerError(
            f"Bot {bot_key} already running for user {user.username} (run {existing.id})", 409
        )

    argv, cwd, env = _launch_plan(spec, ver, user)

    settings = get_settings()
    ts = _utcnow().strftime("%Y%m%d_%H%M%S")
    log_file = settings.log_dir / f"{bot_key}_{ver.version}_{user.username}_{ts}.log"

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

    run = BotRun(
        user_id=user.user_id,
        bot_key=bot_key,
        bot_version=ver.version,
        script_path=str(script_path(spec, ver)),
        pid=proc.pid,
        mode=mode,
        status="running",
        log_file=str(log_file),
        extra={"argv": argv[2:], "cwd": str(cwd)},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    _PROCESSES[run.id] = proc
    logger.info("Started %s %s for %s (pid %s)", bot_key, ver.version, user.username, proc.pid)
    return run


def stop_bot(db: Session, bot_key: str, *, user_id: str | None = None, run_id: int | None = None) -> list[BotRun]:
    """Terminate running processes for the bot (whole process tree)."""
    runs = reconcile_runs(db, bot_key=bot_key, user_id=user_id)
    targets = [r for r in runs if r.status == "running" and (run_id is None or r.id == run_id)]
    if not targets:
        raise BotManagerError(f"No running {bot_key} bot to stop", 404)
    for run in targets:
        _terminate_tree(run)
        run.status = "stopped"
        run.stopped_at = _utcnow()
        _PROCESSES.pop(run.id, None)
    db.commit()
    return targets


def _terminate_tree(run: BotRun) -> None:
    if run.pid is None:
        return
    try:
        parent = psutil.Process(run.pid)
    except psutil.NoSuchProcess:
        return
    procs = [parent]
    try:
        procs += parent.children(recursive=True)
    except psutil.Error:
        pass
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
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
