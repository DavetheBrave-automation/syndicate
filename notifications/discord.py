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


def _post_embed(title: str, description: str, color: int) -> None:
    """Post a Discord embed. Falls back to plain text if embed fails. Never raises."""
    if not SYNDICATE_WEBHOOK:
        return
    try:
        payload = json.dumps({
            "embeds": [{
                "title":       title,
                "description": f"```\n{description}\n```",
                "color":       color,
            }]
        }).encode("utf-8")
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
        # Fallback to plain text
        try:
            post(description)
        except Exception:
            pass


def post_entry_embed(trade_data: dict) -> None:
    """Post a rich entry alert as a Discord embed (blue). Never raises."""
    try:
        from notifications.alert_builder import build_entry_alert, append_to_live_feed  # noqa: PLC0415
        text  = build_entry_alert(trade_data)
        agent = trade_data.get("agent_name", "?")
        _post_embed(f"🎯 NEW TRADE — {agent}", text, 0x3498DB)  # blue
        append_to_live_feed(text, "entry", trade_data)
    except Exception:
        pass


def post_exit_embed(trade_data: dict) -> None:
    """Post a rich exit alert as a Discord embed (green=win, red=loss). Never raises."""
    try:
        from notifications.alert_builder import build_exit_alert, append_to_live_feed  # noqa: PLC0415
        text = build_exit_alert(trade_data)
        won  = float(trade_data.get("pnl") or 0) > 0
        agent = trade_data.get("agent_name", "?")
        color = 0x2ECC71 if won else 0xE74C3C  # green / red
        icon  = "✅" if won else "❌"
        _post_embed(f"{icon} EXIT — {agent}", text, color)
        append_to_live_feed(text, "exit", trade_data)
    except Exception:
        pass
