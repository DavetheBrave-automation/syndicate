"""
signals/fred.py — Federal Reserve macro signals via FRED API
Provides: fed_funds_rate, fed_status, dxy, dxy_status, yield_curve, curve_status
Cache: 4 hours (data doesn't move faster than this)
Free key: fredaccount.stlouisfed.org
"""
import os
import json
import time
import logging
import requests

logger = logging.getLogger("syndicate.signals.fred")

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_SYNDICATE_ROOT, "signals", "cache", "fred.json")
_CACHE_SEC  = 14400   # 4 hours
_BASE       = "https://api.stlouisfed.org/fred/series/observations"


def _fred_key() -> str:
    """Read key from config or env."""
    key = os.getenv("FRED_API_KEY", "")
    if key:
        return key
    try:
        import yaml
        cfg_path = os.path.join(_SYNDICATE_ROOT, "syndicate_config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("signals", {}).get("fred_api_key", "")
    except Exception:
        return ""


def _fetch(series_id: str, api_key: str) -> float | None:
    if not api_key:
        return None
    try:
        r = requests.get(_BASE, params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "limit": 1,
            "sort_order": "desc",
        }, timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        if obs and obs[0]["value"] not in (".", "", None):
            return float(obs[0]["value"])
    except Exception as e:
        logger.warning("[FRED] %s fetch failed: %s", series_id, e)
    return None


def get_all() -> dict:
    # Cache hit
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_ts", 0) < _CACHE_SEC:
                return {k: v for k, v in cached.items() if not k.startswith("_")}
    except Exception:
        pass

    key = _fred_key()

    fed_rate = _fetch("FEDFUNDS", key)
    dxy      = _fetch("DTWEXBGS", key)   # trade-weighted dollar index
    t10      = _fetch("DGS10", key)
    t2       = _fetch("DGS2", key)

    curve = round((t10 or 0) - (t2 or 0), 3) if t10 and t2 else None

    result = {
        "fed_funds_rate": fed_rate,
        "fed_status":     (
            "HAWKISH" if (fed_rate or 0) > 4.0
            else "DOVISH" if (fed_rate or 0) < 2.0
            else "NEUTRAL" if fed_rate else "UNKNOWN"
        ),
        "dxy":            dxy,
        "dxy_status":     (
            "RISING" if (dxy or 100) > 102
            else "FALLING" if (dxy or 100) < 98
            else "FLAT" if dxy else "UNKNOWN"
        ),
        "yield_curve":    curve,
        "curve_status":   (
            "INVERTED" if curve is not None and curve < 0
            else "NORMAL" if curve is not None
            else "UNKNOWN"
        ),
    }

    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({**result, "_ts": time.time()}, f)
    except Exception as e:
        logger.warning("[FRED] cache write failed: %s", e)

    return result
