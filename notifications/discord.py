"""
notifications/discord.py — Discord webhook poster for The Syndicate.

Simple text poster using urllib.request only. Reads webhook from syndicate_config.yaml. Fail silently.
"""

import json
import urllib.request
import yaml as _yaml
import os as _os

_cfg_path = _os.path.join(_os.path.dirname(__file__), '..', 'syndicate_config.yaml')
_n = _yaml.safe_load(open(_cfg_path)).get('notifications', {})
SYNDICATE_WEBHOOK = _n.get('discord_webhook', '')


def post(message: str, emoji: str = "🎯") -> None:
    """Fire-and-forget text post to Syndicate Discord channel. Never raises."""
    if not SYNDICATE_WEBHOOK:
        return
    try:
        payload = json.dumps({"content": f"{emoji} **SYNDICATE** | {message}"}).encode("utf-8")
        req = urllib.request.Request(
            SYNDICATE_WEBHOOK,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com, 1.0)",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
