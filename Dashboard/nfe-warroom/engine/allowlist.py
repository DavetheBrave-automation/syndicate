"""Ticker allowlist / blocklist enforcement.

Reference dashboard hard-blocks KXMVE*, KXCROSS*, parlays — these have
no valid signal model. We replicate that and add a positive allowlist
derived from the asset_class detector.
"""
from app.config import BLOCKED_PREFIXES, ASSET_CLASS_PREFIXES


def asset_class_for(ticker: str) -> str | None:
    """Return asset class name or None if ticker is not allowlisted."""
    for prefix, cls in ASSET_CLASS_PREFIXES.items():
        if ticker.startswith(prefix):
            return cls
    return None


def is_blocked(ticker: str) -> bool:
    return any(ticker.startswith(p) for p in BLOCKED_PREFIXES)


def is_tradeable(ticker: str) -> bool:
    if is_blocked(ticker):
        return False
    return asset_class_for(ticker) is not None
