"""
signals/fng.py — Bitcoin Fear & Greed Index
Source: alternative.me (free, no API key required)
Cache: 1 hour
"""
import os
import json
import time
import logging
import requests

logger = logging.getLogger("syndicate.signals.fng")

_SYNDICATE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_SYNDICATE_ROOT, "signals", "cache", "fng.json")
_CACHE_SEC  = 3600   # 1 hour
_URL        = "https://api.alternative.me/fng/?limit=1"


def get_all() -> dict:
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_ts", 0) < _CACHE_SEC:
                return {k: v for k, v in cached.items() if not k.startswith("_")}
    except Exception:
        pass

    try:
        r = requests.get(_URL, timeout=10)
        r.raise_for_status()
        data  = r.json()["data"][0]
        value = int(data["value"])
        label = data["value_classification"]

        if value < 20:
            status = "EXTREME_FEAR"
        elif value < 40:
            status = "FEAR"
        elif value < 60:
            status = "NEUTRAL"
        elif value < 80:
            status = "GREED"
        else:
            status = "EXTREME_GREED"

        result = {
            "fng_value":  value,
            "fng_label":  label,
            "fng_status": status,
        }

        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({**result, "_ts": time.time()}, f)

        return result

    except Exception as e:
        logger.warning("[FNG] fetch failed: %s", e)
        return {"fng_value": 50, "fng_label": "Neutral", "fng_status": "NEUTRAL"}
