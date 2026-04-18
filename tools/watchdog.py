"""
watchdog.py — Syndicate dual-process health monitor.

Monitors BOTH the engine (main.py) and the gate (wake_syndicate.ps1).

Engine detection:  log mtime stale > DEAD_THRESHOLD (150s)
Gate detection:    process dead OR heartbeat file mtime stale > GATE_HB_THRESHOLD (180s)

Restart backoff:   0s, 30s, 120s, 300s — then alert-only after 4 attempts.
Gate died twice in one session on 2026-04-17 with zero detection. This is the fix.
"""

import os
import sys
import time
import logging
import subprocess
import urllib.request
import urllib.parse
import json

# ---------------------------------------------------------------------------
# Config — Syndicate specific
# ---------------------------------------------------------------------------

REPO_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON          = r"C:\Python314\python.exe"
MAIN_PY         = os.path.join(REPO_ROOT, "main.py")
GATE_PS1        = os.path.join(REPO_ROOT, "intelligence", "wake_syndicate.ps1")

ENGINE_LOG      = os.path.join(REPO_ROOT, "logs", "syndicate.log")
GATE_LOG        = os.path.join(REPO_ROOT, "logs", "wake_syndicate.log")
GATE_ERR_LOG    = os.path.join(REPO_ROOT, "logs", "wake_syndicate_err.log")
GATE_HEARTBEAT  = os.path.join(REPO_ROOT, "logs", "gate_heartbeat.json")
WATCHDOG_LOG    = os.path.join(REPO_ROOT, "logs", "watchdog.log")
CONFIG_YAML     = os.path.join(REPO_ROOT, "syndicate_config.yaml")

PID_FILE        = os.path.join(REPO_ROOT, "syndicate.pid")

DEAD_THRESHOLD      = 150   # engine: seconds of log staleness before declared dead
GATE_HB_THRESHOLD   = 180   # gate: seconds of heartbeat staleness before declared dead
CHECK_INTERVAL      = 30    # seconds between health checks

BACKOFF_DELAYS      = [0, 30, 120, 300]   # seconds to wait before each restart attempt

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

os.makedirs(os.path.join(REPO_ROOT, "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)-8s watchdog -- %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler(WATCHDOG_LOG, encoding="utf-8"),
    ],
)
logger = logging.getLogger("watchdog")

# ---------------------------------------------------------------------------
# Telegram (stdlib only)
# ---------------------------------------------------------------------------

def _load_telegram_creds():
    try:
        import yaml
        with open(CONFIG_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        n = cfg.get("notifications", {})
        return n.get("telegram_bot_token", ""), n.get("telegram_chat_id", "")
    except Exception as e:
        logger.warning("Could not load telegram creds: %s", e)
        return "", ""


def _telegram(msg: str) -> None:
    try:
        token, chat_id = _load_telegram_creds()
        if not token or not chat_id:
            return
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
        req  = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)

# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------

def _get_backoff_delay(restart_count: int) -> int:
    """Return seconds to wait before this restart. -1 = alert-only mode."""
    idx = restart_count - 1
    if idx < len(BACKOFF_DELAYS):
        return BACKOFF_DELAYS[idx]
    return -1   # exceeded all retries — alert only

# ---------------------------------------------------------------------------
# Engine restart
# ---------------------------------------------------------------------------

def _kill_old_process() -> None:
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE) as f:
            old_pid = int(f.read().strip())
        result = subprocess.run(
            ["taskkill", "/PID", str(old_pid), "/T"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            logger.info("[Engine] Killed old process PID %d.", old_pid)
        else:
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid), "/T"],
                           capture_output=True, timeout=5)
        time.sleep(2)
    except Exception as e:
        logger.warning("[Engine] Could not kill old process: %s", e)
    try:
        os.remove(PID_FILE)
    except Exception:
        pass


def _restart_engine(restart_count: int) -> bool:
    """Kill old process, apply backoff, spawn main.py. Returns False if alert-only mode."""
    delay = _get_backoff_delay(restart_count)
    if delay < 0:
        msg = f"[Engine] ALERT-ONLY — exceeded {len(BACKOFF_DELAYS)} restart attempts. Manual intervention required."
        logger.critical(msg)
        _telegram(msg)
        return False

    if delay > 0:
        logger.info("[Engine] Backoff %ds before restart #%d.", delay, restart_count)
        time.sleep(delay)

    _kill_old_process()
    try:
        CREATE_NO_WINDOW      = 0x08000000
        CREATE_NEW_PROC_GROUP = 0x00000200
        subprocess.Popen(
            [PYTHON, MAIN_PY],
            cwd=REPO_ROOT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROC_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[Engine] Restarted (attempt #%d).", restart_count)
        return True
    except Exception as e:
        logger.error("[Engine] Restart failed: %s", e)
        return False

# ---------------------------------------------------------------------------
# Gate restart
# ---------------------------------------------------------------------------

def _restart_gate(restart_count: int) -> bool:
    """Apply backoff, spawn wake_syndicate.ps1. Returns False if alert-only mode."""
    delay = _get_backoff_delay(restart_count)
    if delay < 0:
        msg = f"[Gate] ALERT-ONLY — exceeded {len(BACKOFF_DELAYS)} restart attempts. Manual intervention required."
        logger.critical(msg)
        _telegram(msg)
        return False

    if delay > 0:
        logger.info("[Gate] Backoff %ds before restart #%d.", delay, restart_count)
        time.sleep(delay)

    try:
        CREATE_NO_WINDOW      = 0x08000000
        CREATE_NEW_PROC_GROUP = 0x00000200
        gate_stdout = open(GATE_LOG,     "a", encoding="utf-8")
        gate_stderr = open(GATE_ERR_LOG, "a", encoding="utf-8")
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", GATE_PS1],
            cwd=REPO_ROOT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROC_GROUP,
            stdout=gate_stdout,
            stderr=gate_stderr,
        )
        logger.info("[Gate] Restarted (attempt #%d).", restart_count)
        return True
    except Exception as e:
        logger.error("[Gate] Restart failed: %s", e)
        return False

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    logger.info(
        "Watchdog started. Engine threshold=%ds Gate threshold=%ds interval=%ds",
        DEAD_THRESHOLD, GATE_HB_THRESHOLD, CHECK_INTERVAL,
    )
    _telegram("[Syndicate] Watchdog online (dual-process monitoring: engine + gate).")

    engine_last_good = time.time()
    gate_last_good   = time.time()
    engine_restarts  = 0
    gate_restarts    = 0

    # If gate heartbeat doesn't exist yet, give it 3 minutes to write first one
    if not os.path.exists(GATE_HEARTBEAT):
        gate_last_good = time.time()

    while True:
        time.sleep(CHECK_INTERVAL)

        # ── Engine health ────────────────────────────────────────────────────
        try:
            if not os.path.exists(ENGINE_LOG):
                logger.warning("[Engine] Log file missing.")
            else:
                mtime = os.path.getmtime(ENGINE_LOG)
                if mtime > engine_last_good:
                    engine_last_good = mtime

            engine_stale = time.time() - engine_last_good
            if engine_stale > DEAD_THRESHOLD:
                engine_restarts += 1
                msg = (
                    f"[Engine] DEAD — log stale {engine_stale:.0f}s. "
                    f"Restart #{engine_restarts}."
                )
                logger.critical(msg)
                _telegram(msg)
                if _restart_engine(engine_restarts):
                    engine_last_good = time.time()
            else:
                logger.debug("[Engine] OK — log updated %.0fs ago.", engine_stale)

        except Exception as e:
            logger.error("[Engine] Health check error: %s", e)

        # ── Gate health ──────────────────────────────────────────────────────
        try:
            if os.path.exists(GATE_HEARTBEAT):
                hb_mtime = os.path.getmtime(GATE_HEARTBEAT)
                if hb_mtime > gate_last_good:
                    gate_last_good = hb_mtime
                    gate_restarts  = 0   # reset backoff on confirmed healthy heartbeat

            gate_stale = time.time() - gate_last_good
            if gate_stale > GATE_HB_THRESHOLD:
                gate_restarts += 1
                msg = (
                    f"[Gate] DEAD — heartbeat stale {gate_stale:.0f}s. "
                    f"Restart #{gate_restarts}."
                )
                logger.critical(msg)
                _telegram(msg)
                if _restart_gate(gate_restarts):
                    gate_last_good = time.time()
            else:
                logger.debug("[Gate] OK — heartbeat %.0fs ago.", gate_stale)

        except Exception as e:
            logger.error("[Gate] Health check error: %s", e)


if __name__ == "__main__":
    main()
