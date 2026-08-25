"""FRED (Federal Reserve Economic Data) signal pulls. Cached 4 hours."""
import requests
from app import db
from app.config import FRED_API_KEY

CACHE_SEC = 4 * 3600
BASE = "https://api.stlouisfed.org/fred/series/observations"


def _latest(series_id: str):
    """Return latest non-null value for a FRED series."""
    if not FRED_API_KEY:
        return None
    cache_key = f"fred:{series_id}"
    cached = db.cache_get(cache_key, CACHE_SEC)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            BASE,
            params={
                "series_id": series_id,
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 5,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        for obs in data.get("observations", []):
            if obs["value"] not in (".", "", None):
                val = float(obs["value"])
                db.cache_set(cache_key, val)
                return val
    except Exception as e:
        print(f"[fred] error fetching {series_id}: {e}")
    return None


def fed_funds_rate():
    """Effective Federal Funds Rate."""
    return _latest("DFF")


def dxy_proxy():
    """DXY proxy via DTWEXBGS (Trade Weighted U.S. Dollar Index: Broad)."""
    return _latest("DTWEXBGS")


def yield_curve_10y2y():
    """10-Year minus 2-Year Treasury spread. Negative = inverted."""
    return _latest("T10Y2Y")


def get_all():
    """Bundle for the dashboard."""
    ffr = fed_funds_rate()
    dxy = dxy_proxy()
    curve = yield_curve_10y2y()

    # Derive narrative status strings (matching reference dashboard)
    if ffr is not None:
        fed_status = "HAWKISH" if ffr >= 4.0 else "DOVISH" if ffr < 2.0 else "NEUTRAL"
    else:
        fed_status = "UNKNOWN"

    if dxy is not None:
        # Compare against cached prior value to detect trend
        prior = db.cache_get("fred:DTWEXBGS:prior", 30 * 24 * 3600)
        if prior is None or abs(dxy - prior) < 0.1:
            dxy_status = "FLAT"
        else:
            dxy_status = "RISING" if dxy > prior else "FALLING"
        db.cache_set("fred:DTWEXBGS:prior", dxy)
    else:
        dxy_status = "UNKNOWN"

    if curve is not None:
        curve_status = "INVERTED" if curve < 0 else "NORMAL"
    else:
        curve_status = "UNKNOWN"

    return {
        "fed_funds_rate": ffr,
        "fed_status": fed_status,
        "dxy": dxy,
        "dxy_status": dxy_status,
        "yield_curve": curve,
        "curve_status": curve_status,
    }
