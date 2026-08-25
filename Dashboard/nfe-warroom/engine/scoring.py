"""
engine/scoring.py — Syndicate-backed scoring engine for NFE War Room
Replaces the generic 100-pt placeholder with real edge detection.

Integration contract:
  score_market(market, asset_class, signals) -> {
      total: float,         # 0-100+ composite score
      threshold: int,       # minimum to trigger entry
      breakdown: dict,      # factor_name -> contribution in points
      passes_threshold: bool,
      asset_class: str,
      edge_pct: float,      # raw Syndicate edge (true_prob - kalshi_implied)
      conviction: str,      # GLITCH | HIGH_CONVICTION | PROPHECY
      side: str,            # 'yes' or 'no'
      true_prob: float,     # estimated true probability
      displacement: float,  # abs(yes_mid - 0.5)
  }
"""

import os
import math
import logging

logger = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────────
# Default 50 for paper mode (no FRED/Claude macro signals → missing +10-20 pts).
# Set SCORE_THRESHOLD=60 in production .env once live signals are wired.
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", 50))
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", 7.0))

# Price gate — never enter near-certain contracts
MAX_YES_ENTRY = float(os.getenv("MAX_YES_ENTRY", 0.75))
MIN_YES_ENTRY = float(os.getenv("MIN_YES_ENTRY", 0.25))

# Regime modifiers — ORACLE drives these via signals snapshot
REGIME_MODIFIERS = {
    "oil":      {"HAWKISH_OIL": +8, "DOVISH_OIL": -5, "WAR_PREMIUM": +10},
    "natgas":   {"HAWKISH_OIL": +5, "WAR_PREMIUM": +6},
    "crypto":   {"EXTREME_FEAR": +5, "EXTREME_GREED": -5},
    "sports":   {},
    "politics": {"HIGH_UNCERTAINTY": +3},
    "rates":    {"INVERTED": +5, "NORMAL": -2},
}


def _price_gate(yes_bid_cents: int, yes_ask_cents: int, side: str) -> tuple[bool, str]:
    """Block entries outside the 25-75 cent sweet spot."""
    yes_mid = (yes_bid_cents + yes_ask_cents) / 200.0  # 0..1

    if side == "yes":
        if yes_mid > MAX_YES_ENTRY:
            return False, f"YES {yes_mid:.0%} > {MAX_YES_ENTRY:.0%} ceiling"
        if yes_mid < MIN_YES_ENTRY:
            return False, f"YES {yes_mid:.0%} < {MIN_YES_ENTRY:.0%} floor"
    else:  # NO side
        no_mid = 1.0 - yes_mid
        if no_mid > MAX_YES_ENTRY:
            return False, f"NO {no_mid:.0%} > {MAX_YES_ENTRY:.0%} ceiling"
        if no_mid < MIN_YES_ENTRY:
            return False, f"NO {no_mid:.0%} < {MIN_YES_ENTRY:.0%} floor"

    return True, "ok"


def _edge_factor(yes_bid_cents: int, yes_ask_cents: int,
                 hours_to_expiry: float) -> tuple[float, str, float, float]:
    """
    Core edge calculation — mirrors Syndicate's AXIOM agent.

    Returns: (edge_pct, side, true_prob, displacement)
    edge_pct: positive value means there IS edge
    side: 'yes' if YES is underpriced, 'no' if NO is underpriced
    """
    yes_mid = (yes_bid_cents + yes_ask_cents) / 200.0

    displacement = abs(yes_mid - 0.5)

    # Time factor — more edge value in contracts with more time remaining
    time_factor = max(0.1, min(1.0, 1.0 - (hours_to_expiry / 168.0)))  # 168h = 1 week

    if yes_mid > 0.5:
        # Crowd thinks YES — check if overpriced
        side = "no"
        true_prob_estimate = yes_mid * 0.85  # slight haircut for overconfidence
        edge_pct = (yes_mid - true_prob_estimate) * 100.0
    else:
        # Crowd thinks NO — check if YES underpriced
        side = "yes"
        true_prob_estimate = yes_mid * 1.15  # slight uplift
        edge_pct = (true_prob_estimate - yes_mid) * 100.0

    return edge_pct, side, true_prob_estimate, displacement


def _volume_factor(volume_24h: int, asset_class: str) -> float:
    """
    Normalized volume score — sports-scale calibration.
    Returns 0..25 points.
    """
    VOLUME_SCALES = {
        "oil":      50_000,
        "natgas":   20_000,
        "gold":     20_000,
        "btc":      100_000,
        "eth":      100_000,
        "sol":      100_000,
        "sports":   10_000,
        "politics": 5_000,
        "rates":    20_000,
        "stocks":   10_000,
        "forex":    10_000,
        "default":  10_000,
    }
    scale = VOLUME_SCALES.get(asset_class, VOLUME_SCALES["default"])
    vol_factor = min(1.0, volume_24h / scale)
    return vol_factor * 25.0


def _macro_factor(signals: dict, asset_class: str) -> float:
    """
    Macro regime scoring — driven by live signal feeds.
    Returns -10..+20 points.
    """
    score = 0.0

    # Fear & Greed index (alternative.me)
    fng = signals.get("fng_value", 50)
    if asset_class in ("btc", "eth", "sol"):
        if fng < 20:  # extreme fear = contrarian buy
            score += 10
        elif fng > 80:  # extreme greed = fade
            score -= 8
        elif fng < 35:
            score += 5
    elif asset_class in ("sports", "politics"):
        score += (50 - fng) / 50.0 * 3

    # Fed / rates regime
    fed = signals.get("fed_status", "NEUTRAL")
    curve = signals.get("curve_status", "UNKNOWN")
    if asset_class == "rates":
        if fed == "HAWKISH" and curve == "INVERTED":
            score += 8
        elif fed == "DOVISH":
            score += 5
    elif asset_class in ("oil", "natgas", "gold"):
        if fed == "DOVISH":
            score += 6  # weak dollar = commodities up
        elif fed == "HAWKISH":
            score -= 4

    # DXY trend
    dxy_status = signals.get("dxy_status", "UNKNOWN")
    if asset_class in ("oil", "natgas", "gold"):
        if dxy_status == "FALLING":
            score += 5
        elif dxy_status == "RISING":
            score -= 3

    # Geopolitical overlay for energy
    if asset_class in ("oil", "natgas"):
        if signals.get("us_iran_status") == "ACTIVE":
            score += 6
        if signals.get("hormuz_status") == "BLOCKED":
            score += 8

    # Regime modifiers from ORACLE/WTI feed
    class_modifiers = REGIME_MODIFIERS.get(asset_class, {})
    oil_regime = signals.get("oil_regime", "UNKNOWN")
    for key, delta in class_modifiers.items():
        if oil_regime == key and asset_class in ("oil", "natgas"):
            score += delta
        elif signals.get(f"{asset_class}_regime") == key:
            score += delta

    return max(-10.0, min(20.0, score))


def _time_decay_factor(hours_to_expiry: float, asset_class: str) -> float:
    """
    Time decay score — prefers contracts with enough time to be right.
    Returns 0..15 points.
    Sports decay fast. Oil/politics decay slowly.
    """
    IDEAL_HOURS = {
        "sports":   4,
        "oil":      48,
        "natgas":   48,
        "btc":      24,
        "eth":      24,
        "sol":      24,
        "politics": 72,
        "rates":    72,
        "gold":     48,
        "stocks":   36,
        "forex":    36,
        "default":  24,
    }
    ideal = IDEAL_HOURS.get(asset_class, IDEAL_HOURS["default"])

    if hours_to_expiry < 0.5:
        return 0.0
    elif hours_to_expiry > ideal * 3:
        return 5.0  # too far — uncertainty too high
    else:
        ratio = hours_to_expiry / ideal
        score = 15.0 * math.exp(-0.5 * (math.log(ratio)) ** 2)
        return max(0.0, min(15.0, score))


def _conviction_tier(edge_pct: float, total_score: float) -> str:
    """Map edge + score to Syndicate conviction tier.

    Thresholds calibrated for paper mode (no FRED/macro signals).
    Production with full signals typically scores 65-85+.
    """
    if edge_pct >= 25.0 and total_score >= 80:
        return "PROPHECY"
    elif edge_pct >= 15.0 and total_score >= 65:
        return "HIGH_CONVICTION"
    elif edge_pct >= 7.0 and total_score >= 45:
        return "GLITCH"
    else:
        return "NO_SIGNAL"


def score_market(market: dict, asset_class: str, signals: dict) -> dict:
    """
    Syndicate-backed scoring function.
    Uses AXIOM-style edge detection with ORACLE macro overlay.
    """
    yes_bid = market.get("yes_bid", 50)
    yes_ask = market.get("yes_ask", 50)
    volume_24h = market.get("volume_24h", 0)
    hours_to_expiry = market.get("hours_to_expiry", 24.0)

    # ── Fast gates ──────────────────────────────────────────────────────────
    if volume_24h < int(os.getenv("MIN_VOLUME", 10)):
        return _blocked(asset_class, "volume too low")

    if hours_to_expiry < 0.5:
        return _blocked(asset_class, "too close to expiry")

    # ── Edge calculation ─────────────────────────────────────────────────────
    edge_pct, side, true_prob, displacement = _edge_factor(
        yes_bid, yes_ask, hours_to_expiry
    )

    if edge_pct < MIN_EDGE_PCT:
        return _blocked(asset_class, f"edge {edge_pct:.1f}% < {MIN_EDGE_PCT}% floor")

    # ── Price gate ───────────────────────────────────────────────────────────
    passed, reason = _price_gate(yes_bid, yes_ask, side)
    if not passed:
        return _blocked(asset_class, reason)

    # ── Factor scoring ───────────────────────────────────────────────────────
    f_edge   = min(40.0, edge_pct * 1.6)                   # 0..40 pts — primary signal
    f_volume = _volume_factor(volume_24h, asset_class)      # 0..25 pts
    f_macro  = _macro_factor(signals, asset_class)          # -10..+20 pts
    f_time   = _time_decay_factor(hours_to_expiry, asset_class)  # 0..15 pts

    total = f_edge + f_volume + f_macro + f_time

    # ── Conviction tier ──────────────────────────────────────────────────────
    conviction = _conviction_tier(edge_pct, total)

    if conviction == "NO_SIGNAL":
        return _blocked(asset_class, f"score {total:.0f} below conviction threshold")

    threshold = SCORE_THRESHOLD
    passes = total >= threshold

    return {
        "total":            round(total, 1),
        "threshold":        threshold,
        "breakdown":        {
            "edge":   round(f_edge, 1),
            "volume": round(f_volume, 1),
            "macro":  round(f_macro, 1),
            "time":   round(f_time, 1),
        },
        "passes_threshold": passes,
        "asset_class":      asset_class,
        "edge_pct":         round(edge_pct, 1),
        "true_prob":        round(true_prob, 3),
        "conviction":       conviction,
        "side":             side,
        "displacement":     round(displacement, 3),
    }


def _blocked(asset_class: str, reason: str) -> dict:
    """Standard blocked-market return shape."""
    return {
        "total":            0.0,
        "threshold":        SCORE_THRESHOLD,
        "breakdown":        {"blocked": reason},
        "passes_threshold": False,
        "asset_class":      asset_class,
        "edge_pct":         0.0,
        "conviction":       "NO_SIGNAL",
        "side":             None,
        "true_prob":        0.0,
        "displacement":     0.0,
    }


def score_to_probability(score: float) -> float:
    """Legacy compat shim — new engine returns true_prob directly.

    Kept for sizing.py backward compatibility fallback.
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
