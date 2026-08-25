"""Multi-factor scoring engine. 100 pts total. Asset-class-specific weights.

Score factors (per asset class weights — must sum to 100 before any boost):

                  Oil/Gold  Stocks  Forex  Crypto  Sports  Rates  Politics
  Price Edge        25       25      25     25      25      25     25
  Volume/Liq        15       15      15     15      20      15     15
  Macro             20       15      20     15       0      20     10
  Time Decay        10       10      10     10      15      10     10
  Congress          20       25       5      5       0      15     25
  Reserve           10       10      25     30      40      15     15  (filler — distributed
                                                                         to whichever sub-factor
                                                                         the asset class trusts)

Plus:
  Oil Boost         +5 (oil only — additive after total)

Threshold: SCORE_THRESHOLD (60). Effective oil threshold = 60 - 5 = 55.

Each factor function returns a value 0..1 (fraction of max). The engine
multiplies by the factor weight and sums. Returns dict with total + breakdown.
"""
from app.config import SCORE_THRESHOLD, OIL_BOOST


# Per-asset-class factor weights. Each row sums to 100.
WEIGHTS = {
    "oil":      {"price": 25, "volume": 15, "macro": 20, "time": 10, "congress": 20, "reserve": 10},
    "natgas":   {"price": 25, "volume": 15, "macro": 20, "time": 10, "congress": 20, "reserve": 10},
    "gold":     {"price": 25, "volume": 15, "macro": 20, "time": 10, "congress": 20, "reserve": 10},
    "stocks":   {"price": 25, "volume": 15, "macro": 15, "time": 10, "congress": 25, "reserve": 10},
    "forex":    {"price": 25, "volume": 15, "macro": 20, "time": 10, "congress":  5, "reserve": 25},
    "btc":      {"price": 25, "volume": 15, "macro": 15, "time": 10, "congress":  5, "reserve": 30},
    "eth":      {"price": 25, "volume": 15, "macro": 15, "time": 10, "congress":  5, "reserve": 30},
    "sol":      {"price": 25, "volume": 15, "macro": 15, "time": 10, "congress":  5, "reserve": 30},
    "sports":   {"price": 25, "volume": 20, "macro":  0, "time": 15, "congress":  0, "reserve": 40},
    "rates":    {"price": 25, "volume": 15, "macro": 20, "time": 10, "congress": 15, "reserve": 15},
    "politics": {"price": 25, "volume": 15, "macro": 10, "time": 10, "congress": 25, "reserve": 15},
}


# ---------- factor calculators (each returns 0..1) ----------

def f_price_edge(market: dict) -> float:
    """How mispriced is the contract vs. our fair-value estimate?

    Reference dashboard's exact formula is proprietary. This is a defensible
    starting point: spread compression + bid distance from 50¢ midpoint
    weighted by implied edge.

    market keys expected: yes_bid, yes_ask, no_bid, no_ask, last_price (cents 0-100)
    """
    yb, ya = market.get("yes_bid"), market.get("yes_ask")
    last = market.get("last_price")
    if yb is None or ya is None:
        return 0.0
    spread = max(0, ya - yb)
    # Tight spread = real liquidity = real price discovery
    spread_quality = max(0.0, 1.0 - (spread / 10.0))  # 0¢ spread = 1.0, 10¢+ = 0.0

    # Edge: how far is current bid from "fair" (we use last as proxy)
    if last is None or last == 0:
        return spread_quality * 0.5

    # If bid materially below last, that's an entry edge
    edge = max(0.0, (last - yb) / max(last, 1))
    edge = min(edge, 1.0)

    return min(1.0, 0.4 * spread_quality + 0.6 * edge)


def f_volume(market: dict) -> float:
    """Volume / liquidity factor."""
    vol = market.get("volume_24h", 0)
    if vol < 10:
        return 0.0
    if vol >= 1000:
        return 1.0
    # log-ish ramp between 10 and 1000
    import math
    return min(1.0, math.log10(vol) / 3.0)


def f_macro(asset_class: str, signals: dict) -> float:
    """Macro environment alignment for this asset class."""
    if asset_class == "sports":
        return 0.0  # weighted 0 anyway

    score = 0.5  # neutral baseline

    if asset_class in ("oil", "natgas"):
        if signals.get("us_iran_status") == "ACTIVE":
            score += 0.25
        if signals.get("hormuz_status") == "BLOCKED":
            score += 0.25
        if signals.get("dxy_status") == "RISING":
            score -= 0.10  # commodities inverse to DXY

    elif asset_class == "gold":
        if signals.get("us_iran_status") == "ACTIVE":
            score += 0.20
        if signals.get("fed_status") == "HAWKISH":
            score -= 0.05
        if signals.get("dxy_status") == "RISING":
            score -= 0.10

    elif asset_class in ("btc", "eth", "sol"):
        if "NEGATIVE" in str(signals.get("funding_status", "")):
            score += 0.30
        if "FEAR" in str(signals.get("fng_status", "")):
            score += 0.20
        elif "GREED" in str(signals.get("fng_status", "")):
            score -= 0.20
        if signals.get("fed_status") == "HAWKISH":
            score -= 0.10

    elif asset_class == "stocks":
        if signals.get("fed_status") == "HAWKISH":
            score -= 0.15
        if signals.get("curve_status") == "INVERTED":
            score += 0.10

    elif asset_class == "forex":
        if signals.get("dxy_status") == "RISING":
            score += 0.20
        if "HIK" in str(signals.get("boj_status", "")):
            score += 0.20

    elif asset_class == "rates":
        if signals.get("fed_status") == "HAWKISH":
            score += 0.25
        if signals.get("curve_status") == "INVERTED":
            score += 0.15

    elif asset_class == "politics":
        # Politics largely isolated from macro
        pass

    return max(0.0, min(1.0, score))


def f_time_decay(market: dict) -> float:
    """Time-to-expiry factor.

    Sweet spot is 1d–7d to expiry. Too far = capital tied up; too close
    = high gamma risk and forced exit penalties.
    """
    hours = market.get("hours_to_expiry")
    if hours is None or hours <= 0:
        return 0.0
    if hours < 6:
        return 0.10  # forced exit window — heavy penalty
    if hours < 24:
        return 0.50
    if hours <= 72:
        return 1.00
    if hours <= 168:  # 1 week
        return 0.85
    if hours <= 720:  # 1 month
        return 0.50
    return 0.20


def f_congress(asset_class: str, signals: dict) -> float:
    """Congressional trade alignment."""
    sectors = signals.get("congress_sectors") or []
    if not sectors:
        return 0.0

    # Map asset_class to congress sector keywords
    keywords = {
        "oil": ("oil", "energy", "xom", "cvx"),
        "gold": ("gold", "miner", "metals"),
        "stocks": ("equities", "tech", "spy", "qqq"),
        "rates": ("treasury", "tlt", "rates"),
        "politics": ("defense", "lockheed", "raytheon"),
    }.get(asset_class, ())

    if not keywords:
        return 0.0

    for sector_name, _ in sectors[:3]:
        if any(k in sector_name.lower() for k in keywords):
            return 1.0
    return 0.0


def f_reserve() -> float:
    """Reserve weight bucket — neutral until we add sub-models."""
    return 0.5


# ---------- main entry ----------

def score_market(market: dict, asset_class: str, signals: dict) -> dict:
    """Score a single market. Returns {total, breakdown, passes_threshold}."""
    if asset_class not in WEIGHTS:
        return {"total": 0, "breakdown": {}, "passes_threshold": False}

    w = WEIGHTS[asset_class]

    factors = {
        "price":    f_price_edge(market),
        "volume":   f_volume(market),
        "macro":    f_macro(asset_class, signals),
        "time":     f_time_decay(market),
        "congress": f_congress(asset_class, signals),
        "reserve":  f_reserve(),
    }

    breakdown = {k: round(factors[k] * w[k], 2) for k in w}
    total = sum(breakdown.values())

    # Oil priority boost
    if asset_class in ("oil", "natgas", "gold"):
        total += OIL_BOOST
        breakdown["oil_boost"] = OIL_BOOST

    total = round(total, 2)

    # Effective threshold (oil-class gets the boost effectively lowering threshold)
    threshold = SCORE_THRESHOLD
    passes = total >= threshold

    return {
        "total": total,
        "threshold": threshold,
        "breakdown": breakdown,
        "passes_threshold": passes,
        "asset_class": asset_class,
    }


def score_to_probability(score: float) -> float:
    """Map score (0-100+) to estimated true probability of YES outcome.

    Reference dashboard mapping: 60→38%, 70→44%, 80→56%, 90→74%.
    Linear interpolation between anchors, capped at extremes.
    """
    anchors = [(0, 0.20), (60, 0.38), (70, 0.44), (80, 0.56), (90, 0.74), (100, 0.85)]
    if score <= anchors[0][0]:
        return anchors[0][1]
    if score >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= score <= x1:
            return y0 + (y1 - y0) * (score - x0) / (x1 - x0)
    return 0.20
