"""Position sizing via half-Kelly criterion with conviction multiplier.

For a binary contract priced at c¢ that pays $1.00 if YES (or NO) wins:
  - Win probability p (from Syndicate true_prob, or score_to_probability fallback)
  - Cost per contract c/100 dollars

Kelly fraction:  f* = (p × 100 - c) / (100 - c)
Half-Kelly (variance control): f_half = f* / 2

Conviction multiplier (applied after half-Kelly, before caps):
  PROPHECY        → 1.5×
  HIGH_CONVICTION → 1.0×
  GLITCH          → 0.5×

Then:
  dollars_to_allocate = f_half × multiplier × portfolio_value
  contracts           = floor(dollars_to_allocate / (c / 100))
  contracts           = min(contracts, MAX_CONTRACTS, exposure_cap_check)
"""
from app.config import EXPOSURE_CAP_PCT, SPORTS_CAP_PCT
from engine.scoring import score_to_probability

MAX_CONTRACTS = 100
MAX_DOLLARS_PER_TRADE = 200.00  # absolute hard cap regardless of Kelly

CONVICTION_MULTIPLIERS = {
    "PROPHECY":        1.5,
    "HIGH_CONVICTION": 1.0,
    "GLITCH":          0.5,
}


def kelly_fraction(prob: float, cost_cents: float) -> float:
    """Returns full Kelly fraction. May be negative (no bet)."""
    if cost_cents <= 0 or cost_cents >= 100:
        return 0.0
    c = cost_cents
    f = (prob * 100 - c) / (100 - c)
    return max(0.0, f)


def size_position(
    score: float,
    cost_cents: float,
    portfolio_value: float,
    asset_class: str,
    current_exposure: float = 0.0,
    current_sports_exposure: float = 0.0,
    conviction: str = "GLITCH",
    true_prob: float = None,
) -> dict:
    """Returns sizing decision dict.

    Keys: contracts, dollars, kelly_full, kelly_half, prob, conviction,
          multiplier, blocked_reason

    true_prob: Syndicate-computed probability (preferred). Falls back to
               score_to_probability(score) if not provided.
    conviction: GLITCH | HIGH_CONVICTION | PROPHECY — scales Kelly output.
    """
    if cost_cents <= 0:
        return {"contracts": 0, "blocked_reason": "invalid_cost"}

    # Use Syndicate true_prob if available, else legacy curve
    prob = true_prob if true_prob is not None else score_to_probability(score)
    f_full = kelly_fraction(prob, cost_cents)
    f_half = f_full / 2

    if f_half <= 0:
        return {"contracts": 0, "blocked_reason": "negative_edge",
                "kelly_full": f_full, "kelly_half": f_half, "prob": prob}

    # Apply conviction multiplier before caps
    multiplier = CONVICTION_MULTIPLIERS.get(conviction, 0.5)
    f_sized = f_half * multiplier

    # Dollars to allocate
    target_dollars = f_sized * portfolio_value
    target_dollars = min(target_dollars, MAX_DOLLARS_PER_TRADE)

    # Exposure cap check (overall portfolio)
    max_total_exposure = portfolio_value * EXPOSURE_CAP_PCT
    headroom = max(0.0, max_total_exposure - current_exposure)
    target_dollars = min(target_dollars, headroom)

    # Sports sub-cap
    if asset_class == "sports":
        max_sports = portfolio_value * SPORTS_CAP_PCT
        sports_headroom = max(0.0, max_sports - current_sports_exposure)
        target_dollars = min(target_dollars, sports_headroom)

    if target_dollars <= 0:
        return {"contracts": 0, "blocked_reason": "exposure_cap",
                "kelly_full": f_full, "kelly_half": f_half, "prob": prob}

    # Convert to contracts
    cost_per_contract = cost_cents / 100.0
    contracts = int(target_dollars // cost_per_contract)
    contracts = min(contracts, MAX_CONTRACTS)

    if contracts <= 0:
        return {"contracts": 0, "blocked_reason": "below_min_unit",
                "kelly_full": f_full, "kelly_half": f_half, "prob": prob}

    actual_dollars = contracts * cost_per_contract
    return {
        "contracts":      contracts,
        "dollars":        round(actual_dollars, 2),
        "kelly_full":     round(f_full, 4),
        "kelly_half":     round(f_half, 4),
        "prob":           round(prob, 4),
        "conviction":     conviction,
        "multiplier":     multiplier,
        "blocked_reason": None,
    }
