"""Claude-powered narrative scoring for hard-to-quantify macro signals.

Used for: BOJ/Yen posture, Fed hawkishness narrative, geopolitical risk
(US-Iran, Strait of Hormuz, etc.).

Stub implementation. Wire to Anthropic SDK when ready.
"""
from app import db
from app.config import ANTHROPIC_API_KEY

CACHE_SEC = 6 * 3600


def score_geopolitical():
    """Returns dict of {topic: {status, oil_impact, gold_impact, jpy_impact}}."""
    cached = db.cache_get("llm:geo", CACHE_SEC)
    if cached is not None:
        return cached
    if not ANTHROPIC_API_KEY:
        return None
    # TODO: real Anthropic API call
    # Pull last 24h of headlines (Bing News API or NewsAPI),
    # send to Claude with a structured prompt asking for JSON output.
    return None


def get_all():
    geo = score_geopolitical()
    if geo is None:
        return {
            "us_iran_status": "UNKNOWN",
            "hormuz_status": "UNKNOWN",
            "boj_status": "UNKNOWN",
        }
    return geo
