"""Coinglass crypto funding rates. Stub — wire up when API key available."""
from app import db
from app.config import COINGLASS_API_KEY

CACHE_SEC = 15 * 60


def funding_rate_btc():
    """Returns funding rate as a fraction (e.g. 0.0001 = 0.01%)."""
    if not COINGLASS_API_KEY:
        return None
    cached = db.cache_get("coinglass:btc_funding", CACHE_SEC)
    if cached is not None:
        return cached
    # TODO: real Coinglass API call
    # endpoint: https://open-api.coinglass.com/public/v2/funding
    return None


def get_all():
    fr = funding_rate_btc()
    if fr is None:
        return {"funding_rate": None, "funding_status": "UNKNOWN"}

    if fr < -0.0005:
        status = "VERY NEGATIVE"
    elif fr < 0:
        status = "NEGATIVE"
    elif fr > 0.001:
        status = "VERY POSITIVE"
    else:
        status = "POSITIVE"

    return {"funding_rate": fr, "funding_status": status}
