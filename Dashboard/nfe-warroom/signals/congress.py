"""Congressional trade aggregation. Stub.

Real implementations:
  - Quiver Quantitative API (paid)
  - Capitol Trades scraping (free, fragile)
  - House STOCK Act + Senate eFD direct (free, slow, requires PDF parsing)
"""
from app import db
from app.config import QUIVER_API_KEY

CACHE_SEC = 2 * 3600


def recent_aggregate():
    """Return dict of {sector: net_buying_dollars} for last 30 days."""
    if not QUIVER_API_KEY:
        return None
    cached = db.cache_get("congress:agg", CACHE_SEC)
    if cached is not None:
        return cached
    # TODO: real Quiver API call
    return None


def get_all():
    agg = recent_aggregate()
    if agg is None:
        return {"congress_status": "UNKNOWN", "congress_sectors": []}

    # Identify top 3 net-buying sectors
    sorted_sectors = sorted(agg.items(), key=lambda kv: -kv[1])[:3]
    if not sorted_sectors:
        return {"congress_status": "QUIET", "congress_sectors": []}

    label = "/".join(s[0].upper() for s in sorted_sectors) + " BUYING"
    return {"congress_status": label, "congress_sectors": sorted_sectors}
