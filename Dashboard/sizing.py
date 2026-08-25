"""Position sizing via half-Kelly criterion.

For a binary contract priced at c¢ that pays $1.00 if YES wins:
  - Win probability p (estimated from score)
  - Cost per contract c/100 dollars
  - Net win per contract = (1 - c/100) dollars
  - Net loss per contract = c/100 dollars

Kelly fraction:  f* = (p × b - q) / b
  where b = (1 - c/100) / (c/100)   (payoff odds)
        q = 1 - p

Simplified for binary contracts:
  f* = (p × 100 - c) / (100 - c)

Half-Kelly (recommended by reference dashboard for variance control):
  f_half = f* / 2

Then:
  dollars_to_allocate = f_half × portfolio_value
  contracts          = floor(dollars_to_allocate / (c / 100))
  contracts          = min(contracts, MAX_CONTRACTS, exposure_cap_check)
"""
from app.config import EXPOSURE_CAP_PCT, SPORTS_CAP_PCT
from engine.scoring import score_to_probability

MAX_CONTRACTS = 100
MAX_DOLLARS_PER_TRADE = 200.00  # absolute hard cap regardless of Kelly


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
) -> dict:
    """Returns sizing decision dict.

    Keys: contracts, dollars, kelly_full, kelly_half, prob, blocked_reason
    """
    if cost_cents <= 0:
        return {"contracts": 0, "blocked_reason": "invalid_cost"}

    prob = score_to_probability(score)
    f_full = kelly_fraction(prob, cost_cents)
    f_half = f_full / 2

    if f_half <= 0:
        return {"contracts": 0, "blocked_reason": "negative_edge",
                "kelly_full": f_full, "kelly_half": f_half, "prob": prob}

    # Dollars to allocate
    target_dollars = f_half * portfolio_value
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
        "contracts": contracts,
        "dollars": round(actual_dollars, 2),
        "kelly_full": round(f_full, 4),
        "kelly_half": round(f_half, 4),
        "prob": round(prob, 4),
        "blocked_reason": None,
    }
