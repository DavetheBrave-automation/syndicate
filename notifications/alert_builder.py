"""
notifications/alert_builder.py — Single source of truth for all Syndicate alerts.

All three channels (Telegram, Discord, Dashboard Live Feed) call these builders.
Never crash — always fall back gracefully.

Entry/exit alert format is identical across all channels.
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE_FEED_PATH = os.path.join(_SYNDICATE_ROOT, "logs", "live_feed.json")
_LIVE_FEED_MAX  = 20

logger = logging.getLogger("syndicate.alert_builder")

# ---------------------------------------------------------------------------
# Contract description — Kalshi API cache + regex fallback
# ---------------------------------------------------------------------------

_desc_cache: dict = {}   # {ticker: (description, close_time_str, cached_at)}
_DESC_TTL = 300.0        # 5 minutes


def _fetch_kalshi_metadata(ticker: str) -> Optional[dict]:
    """
    Pull title/subtitle/close_time from Kalshi unauthenticated REST API.
    Returns None on failure. Times out in 4s.
    """
    try:
        import urllib.request
        series = ticker.split("-")[0]
        url = (
            f"https://api.elections.kalshi.com/trade-api/v2/markets"
            f"?series_ticker={series}&limit=50&status=open"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        for m in data.get("markets", []):
            if m.get("ticker") == ticker:
                return m
        # Not found in open — try searching directly
        url2 = (
            f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
        )
        req2 = urllib.request.Request(url2, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req2, timeout=4) as r2:
            return json.loads(r2.read()).get("market", {})
    except Exception:
        return None


def _close_time_to_ct(close_time: str) -> str:
    """
    Convert ISO8601 UTC close_time to 'Apr 16 5PM CT' format.
    EDT = UTC-4. CT = UTC-5 (CDT) / UTC-6 (CST). Use CDT (UTC-5) in spring.
    """
    try:
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        ct = dt - timedelta(hours=5)  # CDT
        hour = ct.hour
        ampm = "AM" if hour < 12 else "PM"
        h12  = hour % 12 or 12
        return ct.strftime(f"%b %d {h12}{ampm} CT")
    except Exception:
        return ""


def _build_description_from_api(ticker: str, title: str, subtitle: Optional[str], close_time: Optional[str]) -> str:
    """Build human-readable description from Kalshi API fields."""
    series = ticker.split("-")[0].upper()

    # Crypto price contracts
    if series in ("KXBTCD", "KXBTCW", "KXETHD", "KXETHW"):
        asset     = "BTC" if "BTC" in series else "ETH"
        direction = "≥" if subtitle and "above" in subtitle.lower() else "≤"
        price_str = ""
        if subtitle:
            m = re.search(r"\$[\d,]+(?:\.\d+)?", subtitle)
            if m:
                price_str = m.group(0)
        time_str = _close_time_to_ct(close_time) if close_time else ""
        parts = [f"{asset} {direction} {price_str}"]
        if time_str:
            parts.append(f"by {time_str}")
        return " ".join(parts)

    # Sports / other: use title, strip "Will " prefix and trailing "?"
    if title:
        desc = title.strip()
        if desc.lower().startswith("will "):
            desc = desc[5:]
        if desc.endswith("?"):
            desc = desc[:-1]
        # Trim to 70 chars at a word boundary
        if len(desc) > 70:
            desc = desc[:67].rsplit(" ", 1)[0] + "…"
        return desc

    return ticker


def describe_contract(ticker: str, metadata: Optional[dict] = None) -> str:
    """
    Return human-readable contract description.
    Priority: caller metadata → API cache → regex fallback → ticker.
    Never raises.
    """
    now = time.monotonic()

    # 1. Try provided metadata
    if metadata:
        try:
            title      = metadata.get("title") or ""
            subtitle   = metadata.get("subtitle")
            close_time = metadata.get("close_time") or metadata.get("expiration_time")
            if title:
                return _build_description_from_api(ticker, title, subtitle, close_time)
        except Exception:
            pass

    # 2. Check cache
    cached = _desc_cache.get(ticker)
    if cached and (now - cached[2]) < _DESC_TTL:
        return cached[0]

    # 3. Fetch from Kalshi API
    try:
        api_data = _fetch_kalshi_metadata(ticker)
        if api_data:
            title      = api_data.get("title") or ""
            subtitle   = api_data.get("subtitle")
            close_time = api_data.get("close_time") or api_data.get("expiration_time")
            desc = _build_description_from_api(ticker, title, subtitle, close_time)
            _desc_cache[ticker] = (desc, close_time or "", now)
            return desc
    except Exception as e:
        logger.debug("[AlertBuilder] API fetch failed for %s: %s", ticker, e)

    # 4. Regex fallback
    desc = _regex_describe(ticker)
    _desc_cache[ticker] = (desc, "", now)
    return desc


def _fmt_strike(raw: str) -> str:
    """Format a strike string like 'T75249.99' → '$75,250'."""
    try:
        val = float(raw.lstrip("T"))
        if val >= 1000:
            return f"${val:,.0f}"
        return f"${val:,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return f"${raw.lstrip('T')}"


def _regex_describe(ticker: str) -> str:
    """Pure regex fallback — no network calls."""
    try:
        parts = ticker.split("-")
        series = parts[0].upper()

        if series in ("KXBTCD", "KXBTCW"):
            # YES side = price ≥ strike on all Kalshi BTC contracts
            return f"BTC ≥ {_fmt_strike(parts[-1])} by settlement"
        if series in ("KXETHD", "KXETHW"):
            return f"ETH ≥ {_fmt_strike(parts[-1])} by settlement"
        if "ATPMATCH" in series or "WTAMATCH" in series:
            return f"Tennis: {parts[-1]}" if len(parts) > 1 else "Tennis match"
        if "PGATOUR" in series:
            player = parts[-1] if len(parts) > 1 else "?"
            return f"PGA Tour: {player}"
        if series.startswith("KXFED"):
            strike = parts[-1].lstrip("T") if len(parts) > 1 else "?"
            return f"Fed funds rate above {strike}%"
        if series.startswith("KXCPI"):
            return f"CPI: {parts[-1]}" if len(parts) > 1 else "CPI reading"
        if "NBA" in series:
            return "NBA Championship"
        if "MLB" in series:
            return "MLB Championship"
        if "NHL" in series:
            return "NHL Championship"
        if "WTI" in series or "OIL" in series:
            return f"Oil ≥ {_fmt_strike(parts[-1])}/bbl" if len(parts) > 1 else "Oil price"
    except Exception:
        pass
    return ticker


# ---------------------------------------------------------------------------
# Tier emoji
# ---------------------------------------------------------------------------

def get_tier_emoji(tier: str) -> str:
    _MAP = {
        "probation": "🥉",
        "qualified":  "🥈",
        "proven":     "🏆",
        "elite":      "💎",
    }
    return _MAP.get((tier or "").lower(), "🥉")


# ---------------------------------------------------------------------------
# Alert builders
# ---------------------------------------------------------------------------

_BORDER = "━" * 26

def build_entry_alert(trade_data: dict) -> str:
    """
    Build entry alert string. Same output used by Telegram, Discord, Dashboard.

    trade_data keys (all optional with fallbacks):
      ticker, agent_name, agent_tier, conviction_tier, side,
      quantity, price_cents, size_dollars, edge_pct, paper, description
    """
    ticker          = trade_data.get("ticker", "?")
    agent           = trade_data.get("agent_name", "?")
    agent_tier      = (trade_data.get("agent_tier") or "probation").lower()
    conviction      = trade_data.get("conviction_tier", "?")
    side            = (trade_data.get("side") or "yes").upper()
    quantity        = int(trade_data.get("quantity") or 0)
    price_cents     = int(trade_data.get("price_cents") or 0)
    size_dollars    = float(trade_data.get("size_dollars") or (price_cents / 100.0 * quantity))
    edge_pct        = float(trade_data.get("edge_pct") or 0.0)
    paper           = bool(trade_data.get("paper", True))
    description     = trade_data.get("description") or describe_contract(ticker)

    tier_emoji = get_tier_emoji(agent_tier)
    paper_tag  = "[PAPER] " if paper else ""
    now_ct     = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%I:%M %p CT").lstrip("0")
    date_str   = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%b %d %Y")

    chunk_count   = int(trade_data.get("chunk_count") or 1)
    chunk_summary = trade_data.get("chunk_summary")
    if chunk_count > 1 and chunk_summary:
        buy_line = f"💰 BUY {side} × {quantity} contracts @ {price_cents}¢ avg ({chunk_count} orders: {chunk_summary})"
    else:
        buy_line = f"💰 BUY {side} × {quantity} contracts @ {price_cents}¢"

    lines = [
        _BORDER,
        f"🎯 {paper_tag}NEW TRADE | {tier_emoji} {agent}",
        _BORDER,
        "",
        f"📝 {description}",
        buy_line,
        f"📊 Total Stake: ${size_dollars:.2f}",
        f"🎪 Tier: {agent_tier.upper()} • {conviction}",
        f"⚡ Edge: {edge_pct:.1f}%",
        "",
        f"🎫 {ticker}",
        f"⏱  {now_ct} • {date_str}",
        _BORDER,
    ]
    return "\n".join(lines)


def build_exit_alert(trade_data: dict) -> str:
    """
    Build exit alert string.

    trade_data keys (all optional with fallbacks):
      ticker, agent_name, agent_tier, side, quantity,
      entry_price_cents, exit_price_cents, pnl (gross), fees_paid, net_pnl,
      hold_minutes, exit_reason, paper, description
    """
    ticker           = trade_data.get("ticker", "?")
    agent            = trade_data.get("agent_name", "?")
    agent_tier       = (trade_data.get("agent_tier") or "probation").lower()
    side             = (trade_data.get("side") or "yes").upper()
    quantity         = int(trade_data.get("quantity") or 0)
    entry_cents      = int(trade_data.get("entry_price_cents") or 0)
    exit_cents       = int(trade_data.get("exit_price_cents") or 0)
    gross_pnl        = float(trade_data.get("pnl") or 0.0)
    fees_paid        = float(trade_data.get("fees_paid") or 0.0)
    net_pnl          = float(trade_data.get("net_pnl") if trade_data.get("net_pnl") is not None else gross_pnl - fees_paid)
    hold_min         = float(trade_data.get("hold_minutes") or 0.0)
    exit_reason      = trade_data.get("exit_reason", "unknown")
    paper            = bool(trade_data.get("paper", True))
    description      = trade_data.get("description") or describe_contract(ticker)

    # P&L formatting — win/loss based on NET P&L
    won         = net_pnl > 0
    result_ico  = "✅ WIN" if won else "❌ LOSS"
    pnl_ico     = "📈" if won else "📉"
    gross_str   = f"+${gross_pnl:.2f}" if gross_pnl >= 0 else f"-${abs(gross_pnl):.2f}"
    net_str     = f"+${net_pnl:.2f}"   if net_pnl   >= 0 else f"-${abs(net_pnl):.2f}"
    entry_cost  = entry_cents / 100.0 * quantity
    net_pct     = (net_pnl / entry_cost * 100) if entry_cost > 0 else 0.0
    net_pct_str = f"+{net_pct:.1f}%" if net_pnl >= 0 else f"{net_pct:.1f}%"

    # Hold duration
    if hold_min >= 60:
        hold_str = f"{hold_min/60:.1f}h"
    else:
        hold_str = f"{hold_min:.0f}m"

    tier_emoji = get_tier_emoji(agent_tier)
    paper_tag  = "[PAPER] " if paper else ""

    chunk_count   = int(trade_data.get("chunk_count") or 1)
    chunk_summary = trade_data.get("chunk_summary")
    if chunk_count > 1 and chunk_summary:
        sell_line = f"💵 SOLD {side} × {quantity} @ {exit_cents}¢ ({chunk_count} orders: {chunk_summary})"
    else:
        sell_line = f"💵 SOLD {side} × {quantity} @ {exit_cents}¢"

    # Fee line: only show if fees > 0
    fee_line = f"💸 Gross: {gross_str} | Fees: -${fees_paid:.2f} | Net: {net_str}" if fees_paid > 0 else f"{pnl_ico} P&L: {net_str} ({net_pct_str})"

    lines = [
        _BORDER,
        f"{result_ico} | {paper_tag}{tier_emoji} {agent}",
        _BORDER,
        "",
        f"📝 {description}",
        sell_line,
        fee_line,
        f"📊 Return: {net_pct_str}",
        f"🎯 Entry {entry_cents}¢ → Exit {exit_cents}¢",
        f"⏱  Hold: {hold_str}",
        f"📋 Reason: {exit_reason}",
        "",
        f"🎫 {ticker}",
        _BORDER,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live Feed writer — shared log for Dashboard
# ---------------------------------------------------------------------------

def append_to_live_feed(text: str, feed_type: str, meta: dict) -> None:
    """
    Append an alert to logs/live_feed.json (last 20 entries, newest first).
    Never raises.
    """
    try:
        os.makedirs(os.path.dirname(_LIVE_FEED_PATH), exist_ok=True)

        try:
            with open(_LIVE_FEED_PATH, encoding="utf-8") as f:
                feed = json.load(f)
            if not isinstance(feed, list):
                feed = []
        except Exception:
            feed = []

        entry = {
            "type":      feed_type,
            "text":      text,
            "ticker":    meta.get("ticker", ""),
            "agent":     meta.get("agent_name", ""),
            "pnl":       meta.get("pnl"),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        feed.insert(0, entry)
        feed = feed[:_LIVE_FEED_MAX]

        tmp = _LIVE_FEED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(feed, f, indent=2)
        os.replace(tmp, _LIVE_FEED_PATH)
    except Exception as e:
        logger.debug("[AlertBuilder] live_feed write failed: %s", e)
