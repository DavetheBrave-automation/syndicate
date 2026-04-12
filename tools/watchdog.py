"""
watchdog.py — Syndicate engine health monitor.

Watches logs/syndicate.log mtime. If the log goes stale for > DEAD_THRESHOLD
seconds the engine is considered dead, restarted, and a Telegram alert sent.

Detection lag: CHECK_INTERVAL (30s) + DEAD_THRESHOLD (150s) = ~3 minutes max.
The engine writes a status line every 60s, so 150s gives 2+ missed heartbeats
before we declare it dead.
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
ENGINE_LOG      = os.path.join(REPO_ROOT, "logs", "syndicate.log")
WATCHDOG_LOG    = os.path.join(REPO_ROOT, "logs", "watchdog.log")
CONFIG_YAML     = os.path.join(REPO_ROOT, "syndicate_config.yaml")

ENGINE_NAME     = "Syndicate"
DEAD_THRESHOLD  = 150   # seconds — 2+ missed 60s status heartbeats
CHECK_INTERVAL  = 30    # seconds between health checks

# ---------------------------------------------------------------------------
# Logging (watchdog's own log)
# ---------------------------------------------------------------------------

os.makedirs(os.path.join(REPO_ROOT, "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)-8s watchdog — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler(WATCHDOG_LOG, encoding="utf-8"),
    ],
)
logger = logging.getLogger("watchdog")

# ---------------------------------------------------------------------------
# Telegram (stdlib only — no local imports)
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
# Engine restart
# ---------------------------------------------------------------------------

def _restart_engine() -> None:
    """Spawn main.py as a detached hidden process."""
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
        logger.info("[%s] Engine restarted.", ENGINE_NAME)
    except Exception as e:
        logger.error("[%s] Restart failed: %s", ENGINE_NAME, e)
        raise

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    logger.info("[%s] Watchdog started. DEAD_THRESHOLD=%ds CHECK_INTERVAL=%ds",
                ENGINE_NAME, DEAD_THRESHOLD, CHECK_INTERVAL)
    _telegram(f"[{ENGINE_NAME}] Watchdog online.")

    # last_good_ts: last time we saw a fresh log write
    last_good_ts = time.time()
    restart_count = 0

    while True:
        time.sleep(CHECK_INTERVAL)

        try:
            if not os.path.exists(ENGINE_LOG):
                logger.warning("[%s] Log file missing — triggering restart.", ENGINE_NAME)
                raise FileNotFoundError(ENGINE_LOG)

            mtime = os.path.getmtime(ENGINE_LOG)
            if mtime > last_good_ts:
                last_good_ts = mtime   # engine is alive and writing

            stale = time.time() - last_good_ts
            if stale > DEAD_THRESHOLD:
                restart_count += 1
                msg = (
                    f"[{ENGINE_NAME}] Engine dead (log stale {stale:.0f}s). "
                    f"Restart #{restart_count}."
                )
                logger.warning(msg)
                _telegram(msg)
                _restart_engine()
                # Give the engine DEAD_THRESHOLD seconds to start writing before
                # we consider it stale again.
                last_good_ts = time.time()
            else:
                logger.debug("[%s] OK — log updated %.0fs ago.", ENGINE_NAME, stale)

        except Exception as e:
            logger.error("[%s] Health check error: %s", ENGINE_NAME, e)


if __name__ == "__main__":
    main()
