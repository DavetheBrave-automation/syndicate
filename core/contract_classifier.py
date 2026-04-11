"""
contract_classifier.py — Contract classification engine for The Syndicate.

Classification rules (applied in order, most restrictive first):
  SCALP:    days_to_settlement <= 1  AND volume > 25000
  SWING:    days_to_settlement <= 7  AND volume > 10000
  POSITION: days_to_settlement <= 14 AND volume > 5000
  WATCH:    anything else — research only, no buy

Max sizes come from config risk section:
  SCALP    → max_per_trade_scalp    (default 5.00)
  SWING    → max_per_trade_swing    (default 3.00)
  POSITION → max_per_trade_position (default 2.00)
  WATCH    → 0.00
"""

import os
import re
import sys
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Root path — must come before any local imports
# ---------------------------------------------------------------------------

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SYNDICATE_ROOT)

from core.shared_state import MarketData  # noqa: E402 — intentional post-path-insert

logger = logging.getLogger("syndicate.classifier")


# ---------------------------------------------------------------------------
# ContractProfile dataclass
# ---------------------------------------------------------------------------

@dataclass
class ContractProfile:
    ticker: str
    contract_class: str          # SCALP / SWING / POSITION / WATCH
    max_size: float              # max dollars allowed per trade
    days_to_settlement: float
    series_ticker: str
    volume_dollars: float
    spread: float
    classification_reason: str


# ---------------------------------------------------------------------------
# Config loader — mtime-cached (same pattern as Atlas brain.py)
# ---------------------------------------------------------------------------

_cfg_cache: dict | None = None
_cfg_mtime: float = 0.0


def _load_config() -> dict:
    global _cfg_cache, _cfg_mtime
    cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
    try:
        mtime = os.path.getmtime(cfg_path)
        if _cfg_cache is None or mtime > _cfg_mtime:
            with open(cfg_path, encoding="utf-8") as f:
                _cfg_cache = yaml.safe_load(f)
            _cfg_mtime = mtime
            logger.debug("[Classifier] Config reloaded (mtime=%.1f)", mtime)
    except Exception as e:
        logger.error("[Classifier] Config load error: %s", e)
    return _cfg_cache or {}


# ---------------------------------------------------------------------------
# Days-to-settlement helper
# ---------------------------------------------------------------------------

def _days_to_settlement(expiry_str: str) -> float:
    """
    Parse an ISO8601 expiry string and return days from now as a float.

    Handles both:
      - datetime strings: "2025-04-11T16:00:00Z", "2025-04-11T16:00:00+00:00"
      - date strings:     "2025-04-11"

    Returns 999.0 on any parse error.
    """
    if not expiry_str:
        return 999.0

    now = datetime.now(tz=timezone.utc)

    # Try full ISO8601 datetime first
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            expiry_dt = datetime.strptime(expiry_str, fmt).replace(tzinfo=timezone.utc)
            delta = (expiry_dt - now).total_seconds() / 86400.0
            return max(delta, 0.0)
        except ValueError:
            pass

    # Try fromisoformat (Python 3.7+) — handles "+HH:MM" offsets natively
    try:
        expiry_dt = datetime.fromisoformat(expiry_str)
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        delta = (expiry_dt - now).total_seconds() / 86400.0
        return max(delta, 0.0)
    except (ValueError, TypeError):
        pass

    # Try bare date "YYYY-MM-DD" — treat as end-of-day UTC
    try:
        d = date.fromisoformat(expiry_str)
        expiry_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
        delta = (expiry_dt - now).total_seconds() / 86400.0
        return max(delta, 0.0)
    except (ValueError, TypeError):
        pass

    logger.warning("[Classifier] Could not parse expiry '%s' — returning 999.0", expiry_str)
    return 999.0


# ---------------------------------------------------------------------------
# Ticker-date extraction — Kalshi tennis tickers embed the match date
# (e.g. KXATPMATCH-26APR11-... = April 11 2026). The API expiry is set to
# end-of-tournament, so we extract the match date directly from the ticker
# and use whichever is EARLIER: ticker date vs API expiry.
# ---------------------------------------------------------------------------

_TICKER_DATE_RE = re.compile(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})')
_MONTH_MAP = {
    "JAN": 1, "FEB": 2,  "MAR": 3,  "APR": 4,
    "MAY": 5, "JUN": 6,  "JUL": 7,  "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _ticker_days(ticker: str) -> Optional[float]:
    """
    Extract the match date embedded in the ticker (e.g. "26APR11" → 2026-04-11)
    and return days from now to end-of-day UTC for that date.
    Returns None if ticker contains no recognisable date pattern.
    """
    m = _TICKER_DATE_RE.search(ticker.upper())
    if not m:
        return None
    try:
        year  = 2000 + int(m.group(1))
        month = _MONTH_MAP[m.group(2)]
        day   = int(m.group(3))
        d     = date(year, month, day)
        expiry_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
        delta = (expiry_dt - datetime.now(tz=timezone.utc)).total_seconds() / 86400.0
        return max(delta, 0.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Classification thresholds — paper mode uses 1_000 minimum across all classes
# ---------------------------------------------------------------------------

def _get_rules() -> list:
    """Return classification rules with volume thresholds adjusted for paper/live mode."""
    cfg = _load_config()
    paper = cfg.get("syndicate", {}).get("paper_mode", False)
    if paper:
        return [
            ("SCALP",    1,  1_000, "settles in {d:.2f}d (<=1), volume ${v:,.0f} (>1000) [paper]"),
            ("SWING",    7,  1_000, "settles in {d:.2f}d (<=7), volume ${v:,.0f} (>1000) [paper]"),
            ("POSITION", 14, 1_000, "settles in {d:.2f}d (<=14), volume ${v:,.0f} (>1000) [paper]"),
        ]
    return [
        ("SCALP",    1,  25_000, "settles in {d:.2f}d (<=1), volume ${v:,.0f} (>25000)"),
        ("SWING",    7,  10_000, "settles in {d:.2f}d (<=7), volume ${v:,.0f} (>10000)"),
        ("POSITION", 14,  5_000, "settles in {d:.2f}d (<=14), volume ${v:,.0f} (>5000)"),
    ]

_WATCH_REASON_TIME   = "settles in {d:.2f}d (>14 days — too far out)"
_WATCH_REASON_VOLUME = "settles in {d:.2f}d but volume ${v:,.0f} too low for any class"


# ---------------------------------------------------------------------------
# Core classify function
# ---------------------------------------------------------------------------

def classify(
    ticker: str,
    expiry_str: str,
    volume_dollars: float,
    spread: float,
    series_ticker: str,
) -> ContractProfile:
    """
    Classify a contract and return a ContractProfile.

    Rules applied in order (SCALP → SWING → POSITION → WATCH).
    First rule whose day AND volume thresholds are both satisfied wins.
    """
    cfg = _load_config()
    risk = cfg.get("risk", {})

    max_sizes = {
        "SCALP":    float(risk.get("max_per_trade_scalp",    5.00)),
        "SWING":    float(risk.get("max_per_trade_swing",    3.00)),
        "POSITION": float(risk.get("max_per_trade_position", 2.00)),
        "WATCH":    0.00,
    }

    days = _days_to_settlement(expiry_str)

    # If the ticker encodes a match date earlier than the API expiry (Kalshi sets
    # expiry to tournament end, not match day), use the ticker date instead so
    # today's matches (APR11) classify as SCALP rather than POSITION.
    ticker_days = _ticker_days(ticker)
    if ticker_days is not None and ticker_days < days:
        logger.debug(
            "[Classifier] %s: ticker date (%.2fd) < API expiry (%.2fd) — using ticker date",
            ticker, ticker_days, days,
        )
        days = ticker_days

    # Apply rules in order
    for class_name, max_days, min_vol, reason_tmpl in _get_rules():
        if days <= max_days and volume_dollars > min_vol:
            reason = reason_tmpl.format(d=days, v=volume_dollars)
            return ContractProfile(
                ticker=ticker,
                contract_class=class_name,
                max_size=max_sizes[class_name],
                days_to_settlement=days,
                series_ticker=series_ticker,
                volume_dollars=volume_dollars,
                spread=spread,
                classification_reason=reason,
            )

    # WATCH — determine why
    if days > 14:
        reason = _WATCH_REASON_TIME.format(d=days, v=volume_dollars)
    else:
        reason = _WATCH_REASON_VOLUME.format(d=days, v=volume_dollars)

    return ContractProfile(
        ticker=ticker,
        contract_class="WATCH",
        max_size=0.00,
        days_to_settlement=days,
        series_ticker=series_ticker,
        volume_dollars=volume_dollars,
        spread=spread,
        classification_reason=reason,
    )


# ---------------------------------------------------------------------------
# Convenience wrapper for MarketData objects from shared_state
# ---------------------------------------------------------------------------

def classify_market(market: MarketData) -> ContractProfile:
    """
    Classify a MarketData object from shared_state.
    Delegates to classify() — single source of truth for classification logic.
    """
    # Synthesise an ISO expiry string from the pre-computed days_to_settlement
    # so classify() can parse it. Safer than duplicating the rule table.
    from datetime import datetime, timezone, timedelta
    expiry_ts = datetime.now(timezone.utc) + timedelta(days=market.days_to_settlement)
    expiry_str = expiry_ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    profile = classify(
        ticker=market.ticker,
        expiry_str=expiry_str,
        volume_dollars=market.volume_dollars,
        spread=market.spread,
        series_ticker=market.series_ticker,
    )
    return profile
