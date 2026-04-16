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


def post_exit(ticker: str, side: str, qty: int, entry: int, exit_price: int,
              pnl: float, reason: str, agent: str, paper: bool = True) -> None:
    """Fire-and-forget exit notification. Shows actual contract cost for NO positions."""
    prefix = "🧪 [PAPER]" if paper else "✅"
    color = "WIN ✅" if pnl > 0 else "LOSS ❌"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    msg = (
        f"{prefix} EXIT | {color} | {pnl_str} | {ticker}\n"
        f"{side.upper()} {qty}x | Entry: {entry}¢ → Exit: {exit_price}¢\n"
        f"Agent: {agent} | {reason}"
    )
    post(msg)


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
