"""BTC Fear & Greed Index from alternative.me. No API key. Cached 1 hour."""
import requests
from app import db

CACHE_SEC = 3600
URL = "https://api.alternative.me/fng/?limit=1"


def fear_and_greed():
    cached = db.cache_get("fng", CACHE_SEC)
    if cached is not None:
        return cached
    try:
        r = requests.get(URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        latest = data["data"][0]
        result = {
            "value": int(latest["value"]),
            "classification": latest["value_classification"],
        }
        db.cache_set("fng", result)
        return result
    except Exception as e:
        print(f"[fng] error: {e}")
        return None


def get_all():
    fng = fear_and_greed()
    if fng is None:
        return {"fng_value": None, "fng_status": "UNKNOWN"}

    val = fng["value"]
    if val <= 20:
        status = f"{fng['classification'].upper()} — CONTRARIAN BUY"
    elif val >= 80:
        status = f"{fng['classification'].upper()} — CONTRARIAN SELL"
    else:
        status = fng["classification"].upper()

    return {
        "fng_value": val,
        "fng_status": status,
    }
