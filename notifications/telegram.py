"""
notifications/telegram.py — Telegram bot poster for The Syndicate.

urllib.request only. Reads credentials from syndicate_config.yaml. Fail silently.
"""

import json
import logging
import time
import urllib.request
import yaml as _yaml
import os as _os
from datetime import datetime, timezone, timedelta
from typing import Optional

_cfg_path = _os.path.join(_os.path.dirname(__file__), '..', 'syndicate_config.yaml')
_n = _yaml.safe_load(open(_cfg_path)).get('notifications', {})
BOT_TOKEN = _n.get('telegram_bot_token', '')
CHAT_ID   = _n.get('telegram_chat_id', '')

_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
logger = logging.getLogger("syndicate.telegram")


def post(message: str, emoji: str = "🎯") -> None:
    """Fire-and-forget Telegram message. Never raises."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        payload = json.dumps({
            "chat_id": CHAT_ID,
            "text": f"{emoji} SYNDICATE | {message}",
        }).encode("utf-8")
        req = urllib.request.Request(
            _URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.debug("[Telegram] post failed: %s", e)


# ---------------------------------------------------------------------------
# Heartbeat gate — 4x/day only (6AM, 10AM, 2PM, 6PM CT)
# ---------------------------------------------------------------------------

_HEARTBEAT_POST_HOURS_CT = {6, 10, 14, 18}
_last_hb_post_time: float = 0.0


def should_post_heartbeat() -> bool:
    """Return True at most once per hour within the 4 scheduled CT windows."""
    global _last_hb_post_time
    ct_now = datetime.now(timezone.utc) - timedelta(hours=6)
    if ct_now.hour in _HEARTBEAT_POST_HOURS_CT:
        if time.time() - _last_hb_post_time >= 55 * 60:
            _last_hb_post_time = time.time()
            return True
    return False


if __name__ == "__main__":
    post("Bot connected and ready", "✅")
    print("Test message sent.")
